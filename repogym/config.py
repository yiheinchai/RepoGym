"""Configuration and on-disk layout.

Everything RepoGym stores lives under REPOGYM_HOME (default ~/.repogym):

    config.json                 global settings (see DEFAULTS)
    repos/<repo_id>/repo.json   registry of captured repositories
    repos/<repo_id>/episodes/   one JSON per captured episode (a prompt -> stop span)
    repos/<repo_id>/events.jsonl raw scrubbed hook events, for auditing
    tasks/<task_id>/            built, verifiable tasks (the gym)
    queue/                      pending build jobs
    logs/                       builder logs
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from .util import read_json, write_json

ENV_HOME = "REPOGYM_HOME"
ENV_OPT_OUT = "REPOGYM_OPT_OUT"
OPT_OUT_FILE = ".repogym-optout"

DEFAULTS: Dict[str, Any] = {
    # Build tasks automatically (detached background process) when an episode closes.
    "auto_build": True,
    # Run the test suite to discover FAIL_TO_PASS / PASS_TO_PASS. Disable to only record diffs.
    "verify": True,
    # Seconds allowed for one test run during build / verification.
    "test_timeout": 900,
    # Directories that are git-ignored but needed to run tests; symlinked into scratch worktrees.
    "link_dirs": ["node_modules", ".venv", "venv", ".tox", "vendor", "target", ".gradle", ".m2"],
    # Optional shell command run inside a scratch worktree before tests (e.g. "npm ci").
    "setup_cmd": None,
    # Store full (scrubbed) tool inputs in the trajectory, not just summaries.
    "capture_tool_inputs": "summary",  # "summary" | "full" | "none"
    # Ask an LLM to rewrite raw prompts into a clean, solution-free problem statement.
    "llm": {"enabled": False, "model": "claude-opus-5"},
    # Drop files whose path matches these globs from every stored patch.
    "exclude_globs": [
        ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
        "*.keystore", "*credentials*", "*secret*", "*.tfstate", "*.tfstate.*",
    ],
    # Minimum / maximum diff size (lines) for a task to be worth keeping.
    "min_diff_lines": 1,
    "max_diff_lines": 20000,
}


def home() -> Path:
    return Path(os.environ.get(ENV_HOME) or (Path.home() / ".repogym")).expanduser()


def ensure_home() -> Path:
    h = home()
    for sub in ("repos", "tasks", "queue", "logs"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    cfg = h / "config.json"
    if not cfg.exists():
        write_json(cfg, DEFAULTS)
    return h


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    user = read_json(home() / "config.json", {}) or {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merged = dict(cfg[k])
            merged.update(v)
            cfg[k] = merged
        else:
            cfg[k] = v
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    write_json(ensure_home() / "config.json", cfg)


def opted_out(repo_path: Path) -> bool:
    """Consent switch: an engineer or a repo can opt out of capture entirely."""
    if os.environ.get(ENV_OPT_OUT, "").lower() in ("1", "true", "yes"):
        return True
    if (repo_path / OPT_OUT_FILE).exists():
        return True
    return False
