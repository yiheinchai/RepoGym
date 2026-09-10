"""Episode capture: the fast path that runs inside editor hooks.

An *episode* is the span between a user prompt and the agent stopping. We snapshot the
working tree at both ends (see gitsnap) and keep a scrubbed trajectory of tool calls in
between. Everything heavy (running tests, writing the task) happens later in `builder`.

All adapters (Claude Code hooks, Codex notify, the generic wrapper) call into this module.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from . import config, gitsnap
from .scrub import scrub_obj, scrub_text
from .store import Episode, Store
from .testrunners import classify_test_command
from .util import now_iso, truncate

MAX_PROMPT_CHARS = 20000
MAX_TOOLS_PER_EPISODE = 2000


class Capture:
    def __init__(self, store: Optional[Store] = None, cfg: Optional[dict] = None):
        self.store = store or Store()
        self.cfg = cfg or config.load_config()

    # ---- helpers -----------------------------------------------------------------------
    def resolve_repo(self, cwd: os.PathLike) -> Optional[Path]:
        root = gitsnap.find_repo_root(cwd)
        if root is None:
            return None
        if config.opted_out(root):
            return None
        return root

    def _register(self, repo: Path) -> str:
        meta = self.store.register_repo(repo, remote=gitsnap.remote_url(repo))
        return meta["id"]

    def log(self, repo: Path, event: dict) -> None:
        rid = self.store.repo_id_for(repo)
        ev = {"ts": now_iso()}
        ev.update(scrub_obj(event))
        self.store.log_event(rid, ev)

    # ---- episode lifecycle -------------------------------------------------------------
    def begin(self, repo: Path, agent: str, session_id: str, prompt: Optional[str] = None,
              meta: Optional[dict] = None) -> Episode:
        """Start (or extend) the open episode for a session. Snapshots the base state."""
        rid = self._register(repo)
        with self.store.lock(rid):
            ep = self.store.open_episode(rid, session_id)
            if ep is None:
                ep = self.store.new_episode(repo, agent, session_id)
                snap = gitsnap.snapshot(repo, f"{ep.id}-base")
                ep.base = snap.to_dict()
                if meta:
                    ep.meta.update(scrub_obj(meta))
            if prompt:
                ep.prompts.append(truncate(scrub_text(prompt), MAX_PROMPT_CHARS))
            self.store.save_episode(ep)
            return ep

    def tool(self, repo: Path, session_id: str, tool_name: str, tool_input: Optional[dict] = None,
             tool_response=None) -> Optional[Episode]:
        """Append a tool call to the open episode's trajectory."""
        rid = self.store.repo_id_for(repo)
        if not self.store.repo_meta(rid):
            return None
        with self.store.lock(rid):
            ep = self.store.open_episode(rid, session_id)
            if ep is None:
                return None
            if len(ep.tools) >= MAX_TOOLS_PER_EPISODE:
                return ep
            record = {"ts": now_iso(), "tool": tool_name}
            mode = self.cfg.get("capture_tool_inputs", "summary")
            tool_input = tool_input or {}
            cmd = None
            if tool_name in ("Bash", "bash", "shell", "exec_command", "local_shell"):
                cmd = tool_input.get("command") or tool_input.get("cmd")
                if isinstance(cmd, list):
                    cmd = " ".join(str(c) for c in cmd)
                if cmd:
                    record["command"] = truncate(scrub_text(str(cmd)), 4000)
                    fam = classify_test_command(str(cmd))
                    if fam:
                        record["test_runner"] = fam
                        ep.test_commands.append(record["command"])
            path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
            if path:
                record["path"] = self._rel(repo, str(path))
                if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "str_replace_editor"):
                    if record["path"] not in ep.files_touched:
                        ep.files_touched.append(record["path"])
            if tool_input.get("pattern"):
                record["pattern"] = truncate(scrub_text(str(tool_input["pattern"])), 500)
            if mode == "full":
                record["input"] = scrub_obj(_shrink(tool_input))
                if tool_response is not None:
                    record["response"] = scrub_obj(_shrink(tool_response, 4000))
            elif mode == "summary" and tool_response is not None and isinstance(tool_response, dict):
                # Only keep cheap, useful signals about the outcome.
                for key in ("exit_code", "returncode", "interrupted", "is_error"):
                    if key in tool_response:
                        record[key] = tool_response[key]
            ep.tools.append(record)
            self.store.save_episode(ep)
            return ep

    def end(self, repo: Path, session_id: str, reason: str = "stop", enqueue: bool = True,
            meta: Optional[dict] = None) -> Optional[Episode]:
        """Close the open episode: snapshot the final state and queue it for building."""
        rid = self.store.repo_id_for(repo)
        if not self.store.repo_meta(rid):
            return None
        with self.store.lock(rid):
            ep = self.store.open_episode(rid, session_id)
            if ep is None:
                return None
            snap = gitsnap.snapshot(repo, f"{ep.id}-final")
            ep.final = snap.to_dict()
            ep.ended_at = now_iso()
            ep.status = "closed"
            ep.meta["end_reason"] = reason
            if meta:
                ep.meta.update(scrub_obj(meta))
            base_tree = (ep.base or {}).get("tree")
            if base_tree and base_tree == snap.tree:
                ep.status = "skipped"
                ep.note = "no_changes"
                # Drop the snapshot refs; nothing to keep alive.
                gitsnap.delete_snapshot_ref(repo, ep.base["ref"])
                gitsnap.delete_snapshot_ref(repo, snap.ref)
                self.store.save_episode(ep)
                return ep
            self.store.save_episode(ep)
        if enqueue:
            self.store.enqueue(ep)
            if self.cfg.get("auto_build", True):
                spawn_builder(self.store.root)
        return ep

    def end_all_open(self, repo: Path, session_id: Optional[str] = None, reason: str = "session_end") -> List[Episode]:
        rid = self.store.repo_id_for(repo)
        closed = []
        for ep in list(self.store.episodes(rid, status="open")):
            if session_id and ep.session_id != session_id:
                continue
            res = self.end(repo, ep.session_id, reason=reason)
            if res:
                closed.append(res)
        return closed

    @staticmethod
    def _rel(repo: Path, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(repo.resolve()))
        except Exception:
            return path


def _shrink(obj, limit: int = 8000):
    if isinstance(obj, str):
        return truncate(obj, limit)
    if isinstance(obj, list):
        return [_shrink(x, limit) for x in obj[:50]]
    if isinstance(obj, dict):
        return {k: _shrink(v, limit) for k, v in list(obj.items())[:50]}
    return obj


def spawn_builder(home: Path) -> Optional[int]:
    """Start a detached background process that drains the build queue.

    Hooks must return quickly, so the expensive work (test runs) is handed off here.
    """
    if os.environ.get("REPOGYM_NO_SPAWN"):
        return None
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "builder.log", "ab")
    env = dict(os.environ)
    env[config.ENV_HOME] = str(home)
    # Avoid inheriting the agent's own hook context into the worker.
    for k in list(env):
        if k.startswith("CLAUDE_") or k.startswith("CODEX_"):
            env.pop(k, None)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "repogym", "build", "--queue", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
            start_new_session=True, close_fds=True,
        )
        return proc.pid
    except Exception:
        return None
    finally:
        log.close()
