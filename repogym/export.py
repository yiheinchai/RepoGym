"""Export tasks to interchange formats.

- swebench: one JSON object per line with the SWE-bench field names, so the gym plugs into
  existing harnesses (SWE-agent, OpenHands, mini-swe-agent, verifiers...). Extra RepoGym
  fields are kept under non-conflicting names.
- jsonl: full task records (task.json + patches inline).
- bundle: a git bundle with the snapshot commits so tasks can be reproduced on another machine.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Optional

from . import gitsnap
from .store import Store
from .verify import task_files


def _repo_slug(task: dict) -> str:
    remote = (task.get("repo") or {}).get("remote") or ""
    if "github.com" in remote:
        slug = remote.split("github.com", 1)[1].lstrip(":/")
        return slug[:-4] if slug.endswith(".git") else slug
    return (task.get("repo") or {}).get("name") or "local/repo"


def to_swebench(task: dict, task_dir: Path) -> dict:
    files = task_files(task_dir)
    return {
        "instance_id": task["id"],
        "repo": _repo_slug(task),
        "base_commit": task["base_commit"],
        "patch": (task_dir / "source.patch").read_text(encoding="utf-8") if (task_dir / "source.patch").exists() else files["solution"],
        "test_patch": files["test"],
        "problem_statement": files["problem"],
        "hints_text": "",
        "created_at": task.get("created_at"),
        "version": str(task.get("version", 1)),
        "FAIL_TO_PASS": json.dumps(task.get("FAIL_TO_PASS", [])),
        "PASS_TO_PASS": json.dumps(task.get("PASS_TO_PASS", [])),
        "environment_setup_commit": task.get("head_commit") or task["base_commit"],
        # RepoGym extras
        "repogym_tier": task.get("tier"),
        "repogym_runner": task.get("runner"),
        "repogym_test_targets": task.get("test_targets", []),
        "repogym_head_commit": task.get("head_commit"),
        "repogym_base_patch": files["base"],
        "repogym_language": task.get("language"),
        "repogym_source_agent": (task.get("source") or {}).get("agent"),
    }


def export(store: Store, out: Path, fmt: str = "swebench", tiers: Optional[Iterable[str]] = None,
           task_ids: Optional[List[str]] = None) -> int:
    tiers = set(tiers) if tiers else None
    n = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for task in store.tasks():
            if task_ids and task["id"] not in task_ids:
                continue
            if tiers and task.get("tier") not in tiers:
                continue
            tdir = store.task_dir(task["id"])
            if fmt == "swebench":
                rec = to_swebench(task, tdir)
            elif fmt == "jsonl":
                rec = dict(task)
                rec["files"] = task_files(tdir)
                traj = tdir / "trajectory.jsonl"
                rec["trajectory"] = [json.loads(l) for l in traj.read_text(encoding="utf-8").splitlines() if l.strip()] if traj.exists() else []
            else:
                raise ValueError(f"unknown format {fmt}")
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def bundle(store: Store, task_id: str, out: Path) -> Path:
    """Write a git bundle containing the task's base and final snapshot commits."""
    task = store.load_task(task_id)
    repo = Path(task["repo"]["path"])
    refs = [r for r in task.get("snapshot_refs", []) if r]
    if not refs:
        raise RuntimeError("task has no snapshot refs")
    gitsnap.export_bundle(repo, out, refs)
    return out
