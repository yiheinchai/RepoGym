"""On-disk store for repositories, episodes and tasks."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import config
from .util import file_lock, now_iso, read_json, stable_id, write_json, append_jsonl, short_id


@dataclass
class Episode:
    """One unit of agent work: the span from a user prompt to the agent stopping.

    base / final are gitsnap.Snapshot dicts. tools is a scrubbed, ordered trajectory.
    """
    id: str
    repo_id: str
    repo_path: str
    agent: str                     # "claude-code" | "codex" | "wrap" | "manual"
    session_id: str
    started_at: str
    status: str = "open"           # open | closed | built | skipped | failed
    ended_at: Optional[str] = None
    prompts: List[str] = field(default_factory=list)
    base: Optional[dict] = None
    final: Optional[dict] = None
    tools: List[dict] = field(default_factory=list)
    test_commands: List[str] = field(default_factory=list)
    files_touched: List[str] = field(default_factory=list)
    task_id: Optional[str] = None
    note: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Episode":
        known = {f for f in Episode.__dataclass_fields__}
        return Episode(**{k: v for k, v in d.items() if k in known})


class Store:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else config.ensure_home()
        for sub in ("repos", "tasks", "queue", "logs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ---- repos -----------------------------------------------------------------------------
    @staticmethod
    def repo_id_for(path: Path) -> str:
        return stable_id(str(Path(path).resolve()))

    def repo_dir(self, repo_id: str) -> Path:
        return self.root / "repos" / repo_id

    def register_repo(self, path: Path, name: Optional[str] = None, remote: Optional[str] = None) -> dict:
        path = Path(path).resolve()
        rid = self.repo_id_for(path)
        rdir = self.repo_dir(rid)
        meta_path = rdir / "repo.json"
        meta = read_json(meta_path, {}) or {}
        meta.update({
            "id": rid,
            "path": str(path),
            "name": name or meta.get("name") or path.name,
            "remote": remote or meta.get("remote"),
            "updated_at": now_iso(),
        })
        meta.setdefault("created_at", meta["updated_at"])
        write_json(meta_path, meta)
        return meta

    def repos(self) -> List[dict]:
        out = []
        for d in sorted((self.root / "repos").iterdir()) if (self.root / "repos").exists() else []:
            m = read_json(d / "repo.json")
            if m:
                out.append(m)
        return out

    def repo_meta(self, repo_id: str) -> Optional[dict]:
        return read_json(self.repo_dir(repo_id) / "repo.json")

    # ---- events ----------------------------------------------------------------------------
    def log_event(self, repo_id: str, event: dict) -> None:
        append_jsonl(self.repo_dir(repo_id) / "events.jsonl", event)

    # ---- episodes --------------------------------------------------------------------------
    def _episodes_dir(self, repo_id: str) -> Path:
        return self.repo_dir(repo_id) / "episodes"

    def episode_path(self, repo_id: str, episode_id: str) -> Path:
        return self._episodes_dir(repo_id) / f"{episode_id}.json"

    def lock(self, repo_id: str):
        return file_lock(self.repo_dir(repo_id) / ".lock")

    def new_episode(self, repo_path: Path, agent: str, session_id: str) -> Episode:
        rid = self.repo_id_for(repo_path)
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")  # microseconds keep ids sortable
        ep = Episode(id=f"{stamp}-{short_id(6)}",
                     repo_id=rid, repo_path=str(Path(repo_path).resolve()), agent=agent,
                     session_id=session_id, started_at=now_iso())
        return ep

    def save_episode(self, ep: Episode) -> None:
        write_json(self.episode_path(ep.repo_id, ep.id), ep.to_dict())

    def load_episode(self, repo_id: str, episode_id: str) -> Optional[Episode]:
        d = read_json(self.episode_path(repo_id, episode_id))
        return Episode.from_dict(d) if d else None

    def find_episode(self, episode_id: str) -> Optional[Episode]:
        for r in self.repos():
            ep = self.load_episode(r["id"], episode_id)
            if ep:
                return ep
        return None

    def episodes(self, repo_id: Optional[str] = None, status: Optional[str] = None) -> Iterator[Episode]:
        repo_ids = [repo_id] if repo_id else [r["id"] for r in self.repos()]
        for rid in repo_ids:
            d = self._episodes_dir(rid)
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                data = read_json(p)
                if not data:
                    continue
                ep = Episode.from_dict(data)
                if status and ep.status != status:
                    continue
                yield ep

    def open_episode(self, repo_id: str, session_id: str) -> Optional[Episode]:
        """The currently open episode for a session in a repo, if any."""
        candidates = [e for e in self.episodes(repo_id, status="open") if e.session_id == session_id]
        return candidates[-1] if candidates else None

    # ---- build queue -----------------------------------------------------------------------
    def enqueue(self, ep: Episode) -> Path:
        p = self.root / "queue" / f"{ep.repo_id}__{ep.id}.json"
        write_json(p, {"repo_id": ep.repo_id, "episode_id": ep.id, "queued_at": now_iso()})
        return p

    def claim_next(self) -> Optional[dict]:
        """Atomically claim a queued job (rename into .claimed) so multiple workers can coexist."""
        qdir = self.root / "queue"
        for p in sorted(qdir.glob("*.json")):
            claimed = p.with_suffix(".claimed")
            try:
                os.rename(p, claimed)
            except FileNotFoundError:
                continue
            job = read_json(claimed)
            if job:
                job["_path"] = str(claimed)
                return job
            claimed.unlink(missing_ok=True)
        return None

    def finish_job(self, job: dict) -> None:
        p = Path(job.get("_path", ""))
        if p.exists():
            p.unlink()

    def queue_size(self) -> int:
        return len(list((self.root / "queue").glob("*.json")))

    # ---- tasks -----------------------------------------------------------------------------
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir() / task_id

    def tasks(self) -> List[dict]:
        out = []
        if not self.tasks_dir().exists():
            return out
        for d in sorted(self.tasks_dir().iterdir()):
            t = read_json(d / "task.json")
            if t:
                out.append(t)
        return out

    def load_task(self, task_id_or_path: str) -> dict:
        p = Path(task_id_or_path)
        if p.is_dir() and (p / "task.json").exists():
            t = read_json(p / "task.json")
        else:
            t = read_json(self.task_dir(task_id_or_path) / "task.json")
            if not t:
                t = read_json(self.root / "cache" / "tasks" / task_id_or_path / "task.json")
        if not t:
            raise FileNotFoundError(f"task not found: {task_id_or_path}")
        return t

    def delete_task(self, task_id: str) -> None:
        shutil.rmtree(self.task_dir(task_id), ignore_errors=True)
