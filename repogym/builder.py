"""Turn a captured episode into a verifiable task (the async, expensive half of RepoGym).

For an episode with base and final snapshots:

 1. diff the snapshots, drop excluded (secret-looking) files, split into test / source patches
 2. detect language and test runner (what the engineer actually ran wins)
 3. in a scratch worktree at base:  apply test patch -> run tests   (results_before)
                                    apply source patch -> run tests (results_after)
 4. FAIL_TO_PASS = tests failing/absent before and passing after; PASS_TO_PASS = passing in both
 5. write tasks/<task_id>/{task.json, problem.md, solution.patch, test.patch, base.patch, trajectory.jsonl}

Tasks are tiered by the strength of the verifier: verified > suite > unverified.
"""
from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, gitsnap
from .scrub import has_secret, path_excluded, scrub_text
from .store import Episode, Store
from .testrunners import (FAILED, PASSED, RunResult, detect_language, detect_runner, is_test_path, run_tests,
                          split_test_command_targets)
from .util import now_iso, slugify, stable_id, write_json, append_jsonl, truncate
from .verify import TIER_SUITE, TIER_UNVERIFIED, TIER_VERIFIED


class BuildSkipped(Exception):
    pass


def _log(quiet: bool, msg: str) -> None:
    if not quiet:
        print(msg, flush=True)


def build_episode(ep: Episode, store: Store, cfg: Optional[dict] = None, llm: Optional[bool] = None,
                  verify: Optional[bool] = None, quiet: bool = False) -> Optional[dict]:
    cfg = cfg or config.load_config()
    verify = cfg.get("verify", True) if verify is None else verify
    use_llm = cfg.get("llm", {}).get("enabled", False) if llm is None else llm
    try:
        task = _build(ep, store, cfg, use_llm, verify, quiet)
    except BuildSkipped as e:
        ep.status = "skipped"
        ep.note = str(e)
        store.save_episode(ep)
        _log(quiet, f"[repogym] skipped {ep.id}: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        ep.status = "failed"
        ep.note = f"{type(e).__name__}: {e}"
        store.save_episode(ep)
        (store.root / "logs").mkdir(exist_ok=True)
        with open(store.root / "logs" / "build-errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} episode={ep.id}\n{traceback.format_exc()}\n")
        _log(quiet, f"[repogym] failed {ep.id}: {e}")
        return None
    ep.status = "built"
    ep.task_id = task["id"]
    store.save_episode(ep)
    _log(quiet, f"[repogym] built {task['id']} tier={task['tier']} F2P={len(task['FAIL_TO_PASS'])} "
                f"P2P={len(task['PASS_TO_PASS'])}")
    return task


def _build(ep: Episode, store: Store, cfg: dict, use_llm: bool, verify: bool, quiet: bool) -> dict:
    if not ep.base or not ep.final:
        raise BuildSkipped("episode has no base/final snapshot")
    repo = Path(ep.repo_path)
    if not repo.exists():
        raise BuildSkipped("repository path no longer exists")
    base, final = ep.base["commit"], ep.final["commit"]
    if not (gitsnap.commit_exists(repo, base) and gitsnap.commit_exists(repo, final)):
        raise BuildSkipped("snapshot commits missing (gc'd or refs deleted)")

    exclude = cfg.get("exclude_globs", [])
    all_files = gitsnap.changed_files(repo, base, final)
    files = [f for f in all_files if not path_excluded(f, exclude)]
    dropped = [f for f in all_files if f not in files]
    if not files:
        raise BuildSkipped("no changes after exclusions")
    test_files = [f for f in files if is_test_path(f)]
    src_files = [f for f in files if f not in test_files]

    full_patch = gitsnap.diff(repo, base, final, paths=files)
    test_patch = gitsnap.diff(repo, base, final, paths=test_files) if test_files else ""
    src_patch = gitsnap.diff(repo, base, final, paths=src_files) if src_files else ""
    if not full_patch.strip():
        raise BuildSkipped("empty diff")

    n_lines = gitsnap.diff_stat_lines(src_patch or full_patch)
    if n_lines < cfg.get("min_diff_lines", 1):
        raise BuildSkipped(f"diff too small ({n_lines} lines)")
    if n_lines > cfg.get("max_diff_lines", 20000):
        raise BuildSkipped(f"diff too large ({n_lines} lines)")

    prompts = [p for p in ep.prompts if p and p.strip()]
    if not prompts:
        raise BuildSkipped("no user prompt captured")

    language = detect_language(files, repo)
    runner = detect_runner(repo, language, ep.test_commands)

    # Which tests to run: the test files touched by the engineer, else whatever they targeted.
    targets = list(test_files)
    if not targets and ep.test_commands:
        targets = [t for t in split_test_command_targets(ep.test_commands[-1]) if (repo / t).exists()
                   or gitsnap.git(["cat-file", "-e", f"{final}:{t}"], cwd=repo, check=False).returncode == 0]

    before: Optional[RunResult] = None
    after: Optional[RunResult] = None
    tier = TIER_UNVERIFIED
    f2p: List[str] = []
    p2p: List[str] = []
    verify_note = None
    if verify and runner:
        _log(quiet, f"[repogym] verifying {ep.id} with {runner} on {len(targets) or 'all'} target(s)")
        before, after = _run_before_after(repo, base, test_patch, src_patch, runner, targets, cfg)
        tier, f2p, p2p, verify_note = _derive_tier(before, after, test_files)
    elif verify and not runner:
        verify_note = "no test runner detected"
    else:
        verify_note = "verification disabled"

    # Reference patches may need redaction; verification above used the raw diff.
    redacted = has_secret(full_patch)
    title = _title(prompts)
    problem = "\n\n".join(prompts)
    enrichment = None
    if use_llm:
        try:
            from . import llm
            enrichment = llm.enrich(prompts, src_patch, files, language, model=cfg.get("llm", {}).get("model"))
        except Exception as e:  # noqa: BLE001 - LLM is optional polish, never fatal
            verify_note = (verify_note + "; " if verify_note else "") + f"llm failed: {type(e).__name__}"
    if enrichment:
        title = enrichment.get("title") or title
        problem = enrichment.get("problem_statement") or problem

    repo_meta = store.repo_meta(ep.repo_id) or {}
    repo_name = repo_meta.get("name") or repo.name
    task_id = f"{slugify(repo_name, 30)}__{slugify(title, 40)}__{stable_id(base, final, n=8)}"
    started = ep.started_at
    ended = ep.ended_at or now_iso()
    task = {
        "version": 1,
        "id": task_id,
        "title": title,
        "created_at": now_iso(),
        "repo": {"name": repo_name, "path": str(repo), "remote": repo_meta.get("remote")},
        "language": language,
        "base_commit": base,
        "head_commit": ep.base.get("head"),
        "branch": ep.base.get("branch"),
        "base_dirty": bool(ep.base.get("dirty")),
        "final_commit": final,
        "snapshot_refs": [ep.base.get("ref"), ep.final.get("ref")],
        "runner": runner,
        "test_targets": targets,
        "test_files": test_files,
        "source_files": src_files,
        "excluded_files": dropped,
        "diff_lines": n_lines,
        "tier": tier,
        "FAIL_TO_PASS": f2p,
        "PASS_TO_PASS": p2p,
        "verification": {
            "note": verify_note,
            "before": _summ(before),
            "after": _summ(after),
        },
        "redacted": redacted,
        "enrichment": {k: v for k, v in (enrichment or {}).items() if k not in ("problem_statement", "title")} or None,
        "source": {
            "agent": ep.agent,
            "session_id": ep.session_id,
            "episode_id": ep.id,
            "prompts": len(prompts),
            "tool_calls": len(ep.tools),
            "test_commands": ep.test_commands[-5:],
            "started_at": started,
            "ended_at": ended,
            "base_uncertain": bool(ep.meta.get("base_uncertain")),
        },
    }
    tdir = store.task_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    write_json(tdir / "task.json", task)
    (tdir / "problem.md").write_text(scrub_text(problem) + "\n", encoding="utf-8")
    (tdir / "solution.patch").write_text(scrub_text(full_patch), encoding="utf-8")
    (tdir / "test.patch").write_text(scrub_text(test_patch), encoding="utf-8")
    (tdir / "source.patch").write_text(scrub_text(src_patch), encoding="utf-8")
    head = ep.base.get("head")
    base_patch = gitsnap.diff(repo, head, base, exclude=exclude) if head and head != base and ep.base.get("dirty") else ""
    (tdir / "base.patch").write_text(scrub_text(base_patch), encoding="utf-8")
    traj = tdir / "trajectory.jsonl"
    if traj.exists():
        traj.unlink()
    for rec in ep.tools:
        append_jsonl(traj, rec)
    return task


def _run_before_after(repo: Path, base: str, test_patch: str, src_patch: str, runner: str,
                      targets: List[str], cfg: dict) -> Tuple[RunResult, RunResult]:
    timeout = cfg.get("test_timeout", 900)
    with gitsnap.worktree(repo, base) as wt:
        gitsnap.link_ignored_dirs(repo, wt, cfg.get("link_dirs", []))
        if test_patch:
            gitsnap.apply_patch(wt, test_patch)
        existing_targets = [t for t in targets if (wt / t).exists()]
        before = run_tests(runner, wt, existing_targets, timeout=timeout, setup_cmd=cfg.get("setup_cmd"))
        if src_patch:
            gitsnap.apply_patch(wt, src_patch)
        existing_targets = [t for t in targets if (wt / t).exists()]
        after = run_tests(runner, wt, existing_targets, timeout=timeout, setup_cmd=cfg.get("setup_cmd"))
    return before, after


def _derive_tier(before: RunResult, after: RunResult, test_files: List[str]) -> Tuple[str, List[str], List[str], Optional[str]]:
    if after.timed_out:
        return TIER_UNVERIFIED, [], [], "test run timed out with the reference solution"
    if after.returncode == 127:
        return TIER_UNVERIFIED, [], [], "test runner not installed"
    if after.structured:
        f2p = sorted(t for t, s in after.results.items() if s == PASSED and before.results.get(t, FAILED) == FAILED)
        p2p = sorted(t for t, s in after.results.items() if s == PASSED and before.results.get(t) == PASSED)
        after_fail = sorted(t for t, s in after.results.items() if s == FAILED)
        if f2p and not after_fail:
            return TIER_VERIFIED, f2p, p2p, None
        if f2p and after_fail:
            # The engineer's own solution leaves failures: keep the discriminating tests, but note it.
            return TIER_VERIFIED, f2p, p2p, f"reference solution leaves {len(after_fail)} failing test(s)"
        if not f2p and after.returncode == 0 and before.returncode != 0:
            return TIER_SUITE, ["<suite>"], [], "no per-test signal; suite flips fail->pass"
        if not f2p:
            return TIER_UNVERIFIED, [], p2p, "no test fails before and passes after the change"
    # No structured results at all (make test, npm test with a custom script...).
    if after.returncode == 0 and before.returncode != 0:
        return TIER_SUITE, ["<suite>"], [], "exit-code verifier only"
    if after.returncode != 0:
        return TIER_UNVERIFIED, [], [], f"reference solution fails the suite (exit {after.returncode})"
    return TIER_UNVERIFIED, [], [], "suite passes before and after; no discriminating tests"


def _summ(r: Optional[RunResult]) -> Optional[dict]:
    if r is None:
        return None
    return {"returncode": r.returncode, "timed_out": r.timed_out, "duration_s": round(r.duration, 1),
            "tests": r.summary(), "command": r.command[:6]}


def _title(prompts: List[str]) -> str:
    first = prompts[0].strip().splitlines()[0] if prompts[0].strip() else "task"
    first = first.lstrip("/#>- ").strip()
    return truncate(first, 80)


def drain_queue(store: Store, cfg: Optional[dict] = None, quiet: bool = False, llm: Optional[bool] = None,
                verify: Optional[bool] = None, max_jobs: Optional[int] = None) -> int:
    """Build every queued episode. Safe to run from several processes at once."""
    cfg = cfg or config.load_config()
    n = 0
    while True:
        if max_jobs is not None and n >= max_jobs:
            break
        job = store.claim_next()
        if job is None:
            break
        try:
            ep = store.load_episode(job["repo_id"], job["episode_id"])
            if ep is not None and ep.status == "closed":
                build_episode(ep, store, cfg, llm=llm, verify=verify, quiet=quiet)
        finally:
            store.finish_job(job)
        n += 1
    return n


def build_all_closed(store: Store, cfg: Optional[dict] = None, quiet: bool = False, llm: Optional[bool] = None,
                     verify: Optional[bool] = None) -> int:
    n = 0
    for ep in list(store.episodes(status="closed")):
        build_episode(ep, store, cfg, llm=llm, verify=verify, quiet=quiet)
        n += 1
    return n


def merge_session(store: Store, repo_id: str, session_id: str) -> Optional[Episode]:
    """Combine all episodes of a session into one (first base -> last final) for a coarser task."""
    eps = sorted((e for e in store.episodes(repo_id) if e.session_id == session_id and e.base and e.final),
                 key=lambda e: (e.started_at, e.id))
    if len(eps) < 2:
        return None
    first, last = eps[0], eps[-1]
    merged = Episode(id=f"{first.id}-session", repo_id=repo_id, repo_path=first.repo_path, agent=first.agent,
                     session_id=session_id + ":merged", started_at=first.started_at, status="closed",
                     ended_at=last.ended_at, base=first.base, final=last.final)
    for e in eps:
        merged.prompts.extend(e.prompts)
        merged.tools.extend(e.tools)
        merged.test_commands.extend(e.test_commands)
        for f in e.files_touched:
            if f not in merged.files_touched:
                merged.files_touched.append(f)
    merged.meta["merged_from"] = [e.id for e in eps]
    store.save_episode(merged)
    return merged
