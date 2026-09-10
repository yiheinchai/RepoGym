"""OpenAI Codex CLI adapter.

Codex exposes a `notify` hook in ~/.codex/config.toml:

    notify = ["repogym", "codex-notify"]

Codex runs that command after each agent turn with a single JSON argument such as

    {"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...", "cwd": "...",
     "input-messages": ["..."], "last-assistant-message": "..."}

Codex only tells us when a turn *ends*, so the base state of a turn is the final state of
the previous turn in the same thread. For the first turn of a thread we snapshot HEAD plus
whatever the working tree looked like when we were told about the turn, and we mark the
episode `base_uncertain` so the builder can decide (by default it still builds; the diff
between the two snapshots is exactly what the agent changed only if nothing else touched
the tree during the turn).

Newer Codex builds also support Claude-Code-compatible hooks (hooks.json). Those payloads
carry the same field names, so `repogym hook` handles them unchanged; use
`repogym install codex --hooks` to write that file (experimental).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..capture import Capture
from ..util import read_json, write_json

NOTIFY_LINE = 'notify = ["repogym", "codex-notify"]'


def handle_notify(payload: Dict[str, Any], capture: Optional[Capture] = None) -> Optional[dict]:
    capture = capture or Capture()
    if payload.get("type") not in (None, "agent-turn-complete"):
        return None
    cwd = payload.get("cwd") or "."
    repo = capture.resolve_repo(cwd)
    if repo is None:
        return None
    thread = str(payload.get("thread-id") or payload.get("thread_id") or payload.get("session_id") or "codex")
    msgs = payload.get("input-messages") or payload.get("input_messages") or []
    if isinstance(msgs, str):
        msgs = [msgs]
    prompt = "\n\n".join(str(m) for m in msgs if m)

    rid = capture.store.repo_id_for(repo)
    existing = capture.store.open_episode(rid, thread) if capture.store.repo_meta(rid) else None
    if existing is None:
        # First turn we hear about: base is best-effort (see module docstring).
        ep = capture.begin(repo, "codex", thread, prompt=prompt, meta={"base_uncertain": True,
                                                                          "turn_id": payload.get("turn-id")})
    else:
        ep = capture.begin(repo, "codex", thread, prompt=prompt)  # appends prompt to open episode
    ended = capture.end(repo, thread, reason="agent-turn-complete",
                        meta={"last_assistant_message": str(payload.get("last-assistant-message") or "")[:1000]})
    # Immediately open the next episode so the *next* turn has a precise base.
    capture.begin(repo, "codex", thread, prompt=None)
    return ended.to_dict() if ended else None


def main_argv(argv) -> int:
    if not argv:
        raw = sys.stdin.read()
    else:
        raw = argv[0]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    try:
        handle_notify(payload)
    except Exception as e:  # noqa: BLE001
        try:
            from .. import config
            with open(config.ensure_home() / "logs" / "hook-errors.log", "a", encoding="utf-8") as fh:
                fh.write(f"codex-notify: {type(e).__name__}: {e}\n")
        except Exception:
            pass
    return 0


# ---- config management -----------------------------------------------------------------------

def config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def install_notify(path: Optional[Path] = None) -> Path:
    """Add (or replace) the top-level `notify` key in Codex's config.toml."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r"^\s*notify\s*=", text, re.M):
        text = re.sub(r"^\s*notify\s*=.*$", NOTIFY_LINE, text, count=1, flags=re.M)
    else:
        # Top-level keys must precede the first [table]; insert at the very top.
        text = NOTIFY_LINE + "\n" + text
    path.write_text(text, encoding="utf-8")
    return path


def uninstall_notify(path: Optional[Path] = None) -> Path:
    path = path or config_path()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^\s*notify\s*=\s*\[\s*\"repogym\".*$\n?", "", text, flags=re.M)
        path.write_text(text, encoding="utf-8")
    return path


def hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def install_hooks(path: Optional[Path] = None) -> Path:
    """Experimental: Codex hooks.json uses the Claude Code hook schema."""
    from .claude_code import install as cc_install
    path = path or hooks_path()
    cc_install(path, command="repogym hook --agent codex")
    return path
