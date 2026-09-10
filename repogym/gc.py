"""Storage housekeeping.

RepoGym's own store is small (tens of KB per task). The thing that can grow is the *repository's*
object store: every snapshot pins a commit under refs/repogym/. Once an episode has been built the
task directory carries everything needed to reproduce it (head_commit + base.patch + patches), so
the pins can go. Unpinned objects are then reclaimed by git's normal gc.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import config, gitsnap
from .capture import rotate_if_large
from .store import Store

PRUNABLE = ("built", "skipped", "failed")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def prune_snapshots(store: Store, days: Optional[int] = None, dry_run: bool = False) -> List[str]:
    """Unpin snapshot refs of finished episodes older than `days`. Returns the refs removed."""
    cfg = config.load_config()
    days = cfg.get("snapshot_retention_days", 7) if days is None else days
    if days is None or int(days) < 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    removed: List[str] = []
    for ep in list(store.episodes()):
        if ep.status not in PRUNABLE or ep.meta.get("snapshots_pruned"):
            continue
        when = _parse(ep.ended_at) or _parse(ep.started_at)
        if when is None or when > cutoff:
            continue
        repo = Path(ep.repo_path)
        refs = [d.get("ref") for d in (ep.base, ep.final) if d and d.get("ref")]
        if repo.exists():
            for ref in refs:
                if not dry_run:
                    gitsnap.delete_snapshot_ref(repo, ref)
                removed.append(ref)
        if not dry_run:
            ep.meta["snapshots_pruned"] = True
            store.save_episode(ep)
    return removed


def rotate_logs(store: Store, max_mb: Optional[float] = None) -> None:
    cfg = config.load_config()
    max_mb = float(cfg.get("log_max_mb") or 5) if max_mb is None else max_mb
    for p in (store.root / "logs").glob("*.log"):
        rotate_if_large(p, max_mb)
    for r in store.repos():
        rotate_if_large(store.repo_dir(r["id"]) / "events.jsonl", max_mb)


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def disk_usage(store: Store) -> Dict:
    """What RepoGym costs on disk: its store, and the snapshot objects pinned inside each repo."""
    repos = []
    for r in store.repos():
        repo = Path(r["path"])
        if not repo.exists() or not gitsnap.find_repo_root(repo):
            repos.append({"name": r["name"], "path": r["path"], "missing": True})
            continue
        n_refs = len(gitsnap.snapshot_refs(repo))
        n_obj, nbytes = gitsnap.snapshot_footprint(repo)
        repos.append({"name": r["name"], "path": r["path"], "snapshot_refs": n_refs,
                      "snapshot_objects": n_obj, "snapshot_bytes": nbytes})
    return {
        "store_bytes": dir_size(store.root),
        "tasks_bytes": dir_size(store.tasks_dir()),
        "repos": repos,
        "pinned_bytes_total": sum(r.get("snapshot_bytes", 0) for r in repos),
    }


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def housekeeping(store: Store) -> None:
    """Cheap, safe maintenance the builder runs after each drain."""
    try:
        prune_snapshots(store)
        rotate_logs(store)
    except Exception:  # noqa: BLE001 - never let housekeeping break a build
        pass
