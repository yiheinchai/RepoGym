"""Generic adapter: wrap *any* agent command with a before/after snapshot.

    repogym wrap --prompt "Fix the flaky retry test" -- codex exec "Fix the flaky retry test"
    repogym wrap -- aider --message "add pagination to /users"

If --prompt is omitted, the prompt is inferred from the wrapped command's positional
arguments (everything that is not a flag), which works for `codex "..."`, `claude -p "..."`
and `aider --message "..."`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

from ..capture import Capture
from ..util import short_id


def infer_prompt(cmd: List[str]) -> str:
    texts = []
    skip_next = False
    for i, a in enumerate(cmd[1:]):
        if skip_next:
            texts.append(a)
            skip_next = False
            continue
        if a in ("-p", "--prompt", "-m", "--message", "--msg"):
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        if a in ("exec", "run", "chat"):
            continue
        texts.append(a)
    return " ".join(texts).strip()


def run_wrapped(cmd: List[str], prompt: Optional[str] = None, cwd: Optional[Path] = None,
                agent: Optional[str] = None, capture: Optional[Capture] = None) -> int:
    capture = capture or Capture()
    cwd = Path(cwd or os.getcwd())
    repo = capture.resolve_repo(cwd)
    session = f"wrap-{short_id(8)}"
    agent = agent or f"wrap:{Path(cmd[0]).name}"
    prompt = prompt if prompt is not None else infer_prompt(cmd)
    if repo is not None:
        capture.begin(repo, agent, session, prompt=prompt, meta={"command": cmd[0]})
    try:
        rc = subprocess.call(cmd, cwd=str(cwd))
    finally:
        if repo is not None:
            capture.end(repo, session, reason=f"exit:{'?'}")
    return rc
