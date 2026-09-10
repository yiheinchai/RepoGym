"""Small shared helpers: subprocess, json, ids, locking, time."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"command failed ({returncode}): {' '.join(self.cmd)}\n{stderr.strip()}")


def run(
    cmd: Sequence[str],
    cwd: Optional[os.PathLike] = None,
    env: Optional[dict] = None,
    check: bool = True,
    timeout: Optional[float] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a command, capturing text output. Raises CommandError when check and non-zero."""
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def git(args: Sequence[str], cwd: os.PathLike, check: bool = True, env: Optional[dict] = None,
        timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    base_env = dict(os.environ)
    # Never let user hooks or pagers interfere with programmatic git.
    base_env.update({"GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    if env:
        base_env.update(env)
    return run(["git", *args], cwd=cwd, env=base_env, check=check, timeout=timeout)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ts() -> float:
    return time.time()


def short_id(n: int = 10) -> str:
    return uuid.uuid4().hex[:n]


def stable_id(*parts: str, n: int = 12) -> str:
    h = hashlib.sha1("\x00".join(parts).encode("utf-8", "replace")).hexdigest()
    return h[:n]


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic JSON write (write temp file in same dir, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{short_id(6)}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, record: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line + "\n")
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> Iterator[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        return


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    """Cross-process exclusive lock using flock on a sidecar file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TimeoutError(f"could not acquire lock {path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def truncate(text: str, limit: int = 2000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 15] + f"...[+{len(text) - limit + 15}]"


def slugify(text: str, limit: int = 40) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    s = "".join(out).strip("-")
    return s[:limit].rstrip("-") or "task"


def which(name: str) -> Optional[str]:
    from shutil import which as _which
    return _which(name)
