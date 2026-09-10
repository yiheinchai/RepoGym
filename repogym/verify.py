"""Materialize a task's base state, apply patches, run tests, compute reward.

Used by the builder (to discover FAIL_TO_PASS / PASS_TO_PASS), by `repogym verify`, and by the
gym environment at submit time.
"""
from __future__ import annotations

import contextlib
import difflib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from . import config, gitsnap
from .testrunners import FAILED, PASSED, RunResult, run_tests
from .util import CommandError, git, read_json

TIER_VERIFIED = "verified"      # per-test FAIL_TO_PASS discovered; the strongest reward signal
TIER_SUITE = "suite"            # only whole-suite exit code flips fail -> pass
TIER_UNVERIFIED = "unverified"  # no executable signal; reward falls back to gold-patch similarity


def task_files(task_dir: Path) -> dict:
    def rd(name: str) -> str:
        p = task_dir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""
    return {
        "solution": rd("solution.patch"),
        "test": rd("test.patch"),
        "base": rd("base.patch"),
        "problem": rd("problem.md"),
    }


def locate_repo(task: dict, store_root: Optional[Path] = None, repo_override: Optional[Path] = None) -> Optional[Path]:
    """Find a local clone that has (or can reconstruct) the task's base commit."""
    candidates: List[Path] = []
    if repo_override:
        candidates.append(Path(repo_override))
    repo = task.get("repo") or {}
    if repo.get("path"):
        candidates.append(Path(repo["path"]))
    if store_root:
        # Task bundles imported from elsewhere are unpacked under <home>/clones/<repo name>.
        candidates.append(Path(store_root) / "clones" / str(repo.get("name") or ""))
    for c in candidates:
        if c.exists() and gitsnap.find_repo_root(c):
            return gitsnap.find_repo_root(c)
    return None


@contextlib.contextmanager
def materialize(task: dict, repo: Path, cfg: Optional[dict] = None, base_dir: Optional[Path] = None,
                keep: bool = False, task_dir: Optional[Path] = None) -> Iterator[Path]:
    """Yield a scratch checkout of the task's base state (with dependency dirs linked in)."""
    cfg = cfg or config.load_config()
    base_commit = task["base_commit"]
    head_commit = task.get("head_commit")
    files = task_files(task_dir) if task_dir else {"base": ""}
    if gitsnap.commit_exists(repo, base_commit):
        target = base_commit
        need_base_patch = False
    elif head_commit and gitsnap.commit_exists(repo, head_commit):
        target = head_commit
        need_base_patch = True
    else:
        raise RuntimeError(
            f"neither base commit {base_commit[:12]} nor head commit {str(head_commit)[:12]} exist in {repo}. "
            "Fetch the snapshot bundle (`repogym bundle`) or the upstream commit first.")
    with gitsnap.worktree(repo, target, base_dir=base_dir, keep=keep) as wt:
        if need_base_patch and files.get("base"):
            gitsnap.apply_patch(wt, files["base"])
        gitsnap.link_ignored_dirs(repo, wt, cfg.get("link_dirs", []))
        yield wt


def reset_paths(wt: Path, base_commit_in_wt: str, paths: Sequence[str]) -> None:
    """Restore `paths` to the checkout's base state (delete files that did not exist there)."""
    existing, missing = [], []
    for p in paths:
        rc = git(["cat-file", "-e", f"{base_commit_in_wt}:{p}"], cwd=wt, check=False).returncode
        (existing if rc == 0 else missing).append(p)
    if existing:
        git(["checkout", base_commit_in_wt, "--", *existing], cwd=wt)
    for p in missing:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(wt / p)


def run_task_tests(task: dict, wt: Path, cfg: dict, timeout: Optional[float] = None) -> RunResult:
    runner = task.get("runner")
    if not runner:
        raise RuntimeError("task has no test runner")
    targets = task.get("test_targets") or []
    # Only pass targets that exist in the checkout; a deleted test file would abort the run.
    targets = [t for t in targets if (wt / t).exists()]
    return run_tests(runner, wt, targets, timeout=timeout or cfg.get("test_timeout", 900),
                     setup_cmd=task.get("setup_cmd") or cfg.get("setup_cmd"))


def score(task: dict, result: Optional[RunResult], candidate_patch: str = "", gold_patch: str = "",
          partial_credit: bool = True) -> dict:
    """Turn a test run into a reward. Mirrors SWE-bench resolution semantics."""
    tier = task.get("tier", TIER_UNVERIFIED)
    info: Dict = {"tier": tier}
    if tier == TIER_VERIFIED and result is not None:
        f2p = task.get("FAIL_TO_PASS", [])
        p2p = task.get("PASS_TO_PASS", [])
        f2p_pass = [t for t in f2p if result.results.get(t) == PASSED]
        p2p_fail = [t for t in p2p if result.results.get(t) != PASSED]
        success = len(f2p_pass) == len(f2p) and not p2p_fail and len(f2p) > 0
        frac = len(f2p_pass) / len(f2p) if f2p else 0.0
        reward = 1.0 if success else (frac if (partial_credit and not p2p_fail) else 0.0)
        info.update({"success": success, "reward": reward, "fail_to_pass_passed": len(f2p_pass),
                     "fail_to_pass_total": len(f2p), "pass_to_pass_failed": p2p_fail[:50],
                     "returncode": result.returncode, "timed_out": result.timed_out})
        return info
    if tier == TIER_SUITE and result is not None:
        success = result.returncode == 0 and not result.timed_out
        info.update({"success": success, "reward": 1.0 if success else 0.0, "returncode": result.returncode,
                     "timed_out": result.timed_out})
        return info
    # Unverified: the best we can do without tests is similarity to the engineer's own solution.
    sim = patch_similarity(candidate_patch, gold_patch) if gold_patch else 0.0
    info.update({"success": None, "reward": None, "similarity": sim,
                 "note": "no executable verifier for this task; similarity is advisory only"})
    return info


def patch_similarity(a: str, b: str) -> float:
    """Similarity of the *content* lines of two unified diffs (ignores hunk headers)."""
    def body(p: str) -> List[str]:
        out = []
        for line in p.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                out.append(line.strip())
        return out
    la, lb = body(a), body(b)
    if not la and not lb:
        return 1.0
    return difflib.SequenceMatcher(a=la, b=lb, autojunk=False).ratio()


def verify_patch(task: dict, task_dir: Path, candidate_patch: str, repo: Optional[Path] = None,
                 cfg: Optional[dict] = None, timeout: Optional[float] = None, partial_credit: bool = True,
                 store_root: Optional[Path] = None) -> dict:
    """Full verification of a candidate patch against a task: base -> candidate -> hidden tests -> run."""
    cfg = cfg or config.load_config()
    files = task_files(task_dir)
    repo = repo or locate_repo(task, store_root)
    if repo is None:
        raise RuntimeError("no local repository available for this task")
    with materialize(task, repo, cfg, task_dir=task_dir) as wt:
        applied = True
        apply_error = None
        try:
            gitsnap.apply_patch(wt, candidate_patch)
        except CommandError as e:
            applied = False
            apply_error = e.stderr[-2000:]
        result = None
        if applied and task.get("runner") and task.get("tier") in (TIER_VERIFIED, TIER_SUITE):
            # Hidden tests always win over whatever the candidate did to test files.
            base_in_wt = "HEAD"
            test_files = task.get("test_files") or []
            if files["test"]:
                reset_paths(wt, base_in_wt, test_files)
                gitsnap.apply_patch(wt, files["test"])
            result = run_task_tests(task, wt, cfg, timeout)
        info = score(task, result, candidate_patch, files["solution"], partial_credit)
        info["patch_applied"] = applied
        if apply_error:
            info["apply_error"] = apply_error
            info["reward"] = 0.0 if info.get("reward") is not None else None
            info["success"] = False if info.get("success") is not None else None
        if result is not None:
            info["test_summary"] = result.summary()
            info["stdout_tail"] = result.stdout[-4000:]
            info["stderr_tail"] = result.stderr[-2000:]
        return info
