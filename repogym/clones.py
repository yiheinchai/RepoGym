"""Repository clones for machines that only have the task and a git remote (e.g. a training box).

    ~/.repogym/clones/<repo name>/   (no checkout; worktrees are created from it per episode)

`ensure_repo` makes sure the task's head_commit is present (fetching it from the remote if needed)
and restores the exact snapshot commits from `snapshots.bundle` when the task ships one.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from . import gitsnap
from .util import git


def clone_root(store_root: Path) -> Path:
    d = store_root / "clones"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_repo(task: dict, task_dir: Path, store_root: Path, remote_url: Optional[str] = None,
                partial: bool = True) -> Path:
    """Return a local repo that can materialize this task, cloning/fetching as needed."""
    repo_meta = task.get("repo") or {}
    remote_url = remote_url or repo_meta.get("remote")
    name = repo_meta.get("name") or "repo"
    dest = clone_root(store_root) / name
    if not (dest / ".git").exists():
        if not remote_url:
            raise RuntimeError(f"task {task['id']} has no git remote recorded and no local clone exists")
        args = ["clone", "--no-checkout", "--quiet"]
        if partial:
            args.append("--filter=blob:none")  # blobs stream in on demand; keeps clones small
        proc = subprocess.run(["git", *args, remote_url, str(dest)], capture_output=True, text=True)
        if proc.returncode != 0 and partial:
            proc = subprocess.run(["git", "clone", "--no-checkout", "--quiet", remote_url, str(dest)],
                                  capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")
    head = task.get("head_commit")
    if head and not gitsnap.commit_exists(dest, head):
        git(["fetch", "--quiet", "origin", head], cwd=dest, check=False)
        if not gitsnap.commit_exists(dest, head):
            git(["fetch", "--quiet", "--all"], cwd=dest, check=False)
    bundle = task_dir / "snapshots.bundle"
    if bundle.exists() and not gitsnap.commit_exists(dest, task["base_commit"]):
        # Thin bundle: needs head_commit present (fetched above).
        git(["bundle", "unbundle", str(bundle)], cwd=dest, check=False)
        for ref in task.get("snapshot_refs") or []:
            pass  # refs are recorded in the bundle header; unbundle prints them, objects are what we need
        git(["update-ref", f"refs/repogym/imported/{task['id']}-base", task["base_commit"]], cwd=dest, check=False)
        if task.get("final_commit"):
            git(["update-ref", f"refs/repogym/imported/{task['id']}-final", task["final_commit"]], cwd=dest,
                check=False)
    if not gitsnap.commit_exists(dest, task["base_commit"]) and not (head and gitsnap.commit_exists(dest, head)):
        raise RuntimeError(
            f"could not obtain base ({task['base_commit'][:12]}) or head ({str(head)[:12]}) for {task['id']} "
            f"from {remote_url}; push the branch or ship snapshots.bundle")
    return dest
