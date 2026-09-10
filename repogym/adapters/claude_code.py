"""Claude Code adapter.

Claude Code calls each configured hook command with a JSON payload on stdin and the fields
`session_id`, `cwd`, `hook_event_name`, plus event specific ones. We map:

    SessionStart      -> register the repo
    UserPromptSubmit  -> begin (or extend) an episode; snapshot base state
    PostToolUse       -> append tool call to the trajectory (Bash commands reveal how tests run)
    Stop              -> end the episode; snapshot final state; enqueue a build
    SessionEnd        -> close anything still open

Hooks never block the agent: we always exit 0 and print nothing on stdout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..capture import Capture
from ..util import read_json, write_json

HOOK_COMMAND = "repogym hook"
HOOK_EVENTS = {
    "SessionStart": {"matcher": "startup|resume|clear|compact|fork"},
    "UserPromptSubmit": None,
    "PostToolUse": {"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit"},
    "Stop": None,
    "SessionEnd": None,
}
MARKER = "__repogym__"


def handle(payload: Dict[str, Any], capture: Optional[Capture] = None) -> Optional[dict]:
    """Process one hook payload. Returns the affected episode dict (for tests/debugging) or None."""
    capture = capture or Capture()
    event = payload.get("hook_event_name") or payload.get("event") or ""
    cwd = payload.get("cwd") or "."
    session_id = str(payload.get("session_id") or "unknown")
    repo = capture.resolve_repo(cwd)
    if repo is None:
        return None
    agent = payload.get("_agent", "claude-code")

    if event == "SessionStart":
        capture.store.register_repo(repo)
        capture.log(repo, {"event": event, "session_id": session_id, "source": payload.get("source")})
        return None

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or payload.get("user_input") or payload.get("user_prompt") or ""
        # Slash commands and trivial acknowledgements do not make good tasks on their own but are
        # still part of the conversation; keep them, the builder decides what to do.
        ep = capture.begin(repo, agent, session_id, prompt=str(prompt),
                           meta={"transcript_path": payload.get("transcript_path"),
                                 "permission_mode": payload.get("permission_mode")})
        capture.log(repo, {"event": event, "session_id": session_id, "episode": ep.id})
        return ep.to_dict()

    if event in ("PostToolUse", "PostToolUseFailure"):
        ep = capture.tool(repo, session_id, str(payload.get("tool_name") or "unknown"),
                          payload.get("tool_input") or {}, payload.get("tool_response"))
        return ep.to_dict() if ep else None

    if event == "Stop" or event == "SubagentStop":
        if event == "SubagentStop":
            return None  # subagents finish inside the parent's episode
        ep = capture.end(repo, session_id, reason="stop",
                         meta={"last_assistant_message": _short(payload.get("last_assistant_message"))})
        capture.log(repo, {"event": event, "session_id": session_id, "episode": ep.id if ep else None})
        return ep.to_dict() if ep else None

    if event == "SessionEnd":
        closed = capture.end_all_open(repo, session_id=session_id, reason=f"session_end:{payload.get('reason')}")
        capture.log(repo, {"event": event, "session_id": session_id, "closed": [e.id for e in closed]})
        return closed[-1].to_dict() if closed else None

    return None


def _short(text) -> Optional[str]:
    if not text:
        return None
    text = str(text)
    return text[:1000]


def main_stdin() -> int:
    """Entry point for `repogym hook`: read the payload from stdin, never fail the agent."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    try:
        handle(payload)
    except Exception as e:  # noqa: BLE001 - hooks must be silent and non-blocking
        try:
            from .. import config
            log = config.ensure_home() / "logs" / "hook-errors.log"
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{payload.get('hook_event_name')}: {type(e).__name__}: {e}\n")
        except Exception:
            pass
    return 0


# ---- settings.json management --------------------------------------------------------------

def settings_path(scope: str, project_dir: Optional[Path] = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "project":
        return (project_dir or Path.cwd()) / ".claude" / "settings.json"
    if scope == "local":
        return (project_dir or Path.cwd()) / ".claude" / "settings.local.json"
    raise ValueError("scope must be user|project|local")


def _hook_entry(command: str, timeout: int) -> dict:
    return {"type": "command", "command": command, "timeout": timeout, MARKER: True}


def install(path: Path, command: str = HOOK_COMMAND, timeout: int = 120) -> dict:
    """Idempotently add RepoGym hooks to a Claude Code settings file."""
    settings = read_json(path, {}) or {}
    hooks = settings.setdefault("hooks", {})
    for event, opts in HOOK_EVENTS.items():
        groups = hooks.setdefault(event, [])
        # Remove stale RepoGym entries first.
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not h.get(MARKER)]
        groups[:] = [g for g in groups if g.get("hooks")]
        group = {"hooks": [_hook_entry(command, timeout)]}
        if opts and opts.get("matcher"):
            group = {"matcher": opts["matcher"], **group}
        groups.append(group)
    write_json(path, settings)
    return settings


def uninstall(path: Path) -> dict:
    settings = read_json(path, {}) or {}
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = hooks[event]
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not h.get(MARKER)]
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if not hooks and "hooks" in settings:
        del settings["hooks"]
    write_json(path, settings)
    return settings
