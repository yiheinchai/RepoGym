"""Dependency provisioning: the agent starts in a repo whose dependencies are already installed.

The RL policy should be solving the task, not fighting `npm ci`. At build time RepoGym records a
*deps spec* for the task (which package manager, which lockfile, its hash, the install command).
At reset time, `ensure()` provisions that spec once per (repo, lockfile hash) into a cache

    ~/.repogym/deps/<repo_id>/<manager>-<lockhash>/{node_modules | .venv | vendor}

and links it into the scratch worktree. A thousand episodes on the same lockfile share one install.
On the engineer's own machine their real node_modules/.venv are linked instead (see link_dirs).

Supported: npm / pnpm / yarn / bun, pip requirements / pyproject / uv / poetry, bundler, go, cargo.
`setup_cmd` in config or task overrides the install command.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .util import file_lock, now_iso, read_json, write_json

READY = ".ready"
FAILED = ".failed"


def _hash_files(root: Path, names: List[str]) -> str:
    h = hashlib.sha1()
    for n in sorted(names):
        p = root / n
        if p.is_file():
            h.update(n.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _glob(root: Path, pattern: str) -> List[str]:
    return sorted(str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file())


def detect(root: Path, runner: Optional[str] = None) -> Optional[Dict]:
    """Inspect a checkout and describe how to install its dependencies. None = nothing to do."""
    root = Path(root)
    specs: List[Dict] = []

    if (root / "package.json").exists():
        if (root / "pnpm-lock.yaml").exists():
            cmd, lock = ["pnpm", "install", "--frozen-lockfile"], ["pnpm-lock.yaml"]
        elif (root / "yarn.lock").exists():
            cmd, lock = ["yarn", "install", "--frozen-lockfile"], ["yarn.lock"]
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            cmd, lock = ["bun", "install", "--frozen-lockfile"], [n for n in ("bun.lockb", "bun.lock") if (root / n).exists()]
        elif (root / "package-lock.json").exists():
            cmd, lock = ["npm", "ci", "--no-audit", "--no-fund"], ["package-lock.json"]
        else:
            cmd, lock = ["npm", "install", "--no-audit", "--no-fund"], []
        specs.append({"manager": cmd[0], "install": cmd, "lock_files": lock + ["package.json"],
                      "link": "node_modules", "kind": "node"})

    py_lock: List[str] = []
    py_cmd: Optional[List[str]] = None
    if (root / "uv.lock").exists():
        py_cmd, py_lock = ["uv", "sync", "--frozen"], ["uv.lock", "pyproject.toml"]
        manager = "uv"
    elif (root / "poetry.lock").exists():
        py_cmd, py_lock = ["poetry", "install", "--no-interaction"], ["poetry.lock", "pyproject.toml"]
        manager = "poetry"
    else:
        reqs = [n for n in ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "dev-requirements.txt",
                            "requirements-test.txt", "test-requirements.txt") if (root / n).exists()]
        reqs += _glob(root, "requirements/*.txt")
        has_pyproject = (root / "pyproject.toml").exists()
        has_setup = (root / "setup.py").exists() or (root / "setup.cfg").exists()
        if reqs or has_pyproject or has_setup:
            manager = "pip"
            py_lock = reqs + [n for n in ("pyproject.toml", "setup.py", "setup.cfg") if (root / n).exists()]
            py_cmd = ["__pip__"]  # expanded in provision(): venv + pip install -r ... / -e .
    if py_cmd:
        specs.append({"manager": manager, "install": py_cmd, "lock_files": py_lock, "link": ".venv", "kind": "python"})

    if (root / "Gemfile").exists():
        specs.append({"manager": "bundler", "install": ["bundle", "install", "--path", "vendor/bundle"],
                      "lock_files": [n for n in ("Gemfile.lock", "Gemfile") if (root / n).exists()],
                      "link": "vendor", "kind": "ruby"})
    if (root / "go.mod").exists():
        specs.append({"manager": "go", "install": ["go", "mod", "download"], "lock_files": ["go.sum", "go.mod"],
                      "link": None, "kind": "go"})  # global module cache; nothing to link
    if (root / "Cargo.toml").exists():
        specs.append({"manager": "cargo", "install": ["cargo", "fetch"], "lock_files": ["Cargo.lock", "Cargo.toml"],
                      "link": None, "kind": "rust"})

    if not specs:
        return None
    for s in specs:
        s["lock_hash"] = _hash_files(root, s["lock_files"])
    return {"specs": specs, "runner": runner, "detected_at": now_iso()}


def cache_root(store_root: Path, repo_id: str) -> Path:
    d = store_root / "deps" / repo_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(cmd, cwd: Path, env: dict, timeout: float, log: List[str]) -> int:
    log.append("$ " + (" ".join(cmd) if isinstance(cmd, list) else cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, errors="replace",
                              timeout=timeout, shell=isinstance(cmd, str), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        log.append("timed out")
        return 124
    except FileNotFoundError as e:
        log.append(f"not found: {e}")
        return 127
    log.append(proc.stdout[-4000:])
    log.append(proc.stderr[-4000:])
    return proc.returncode


def _provision_python(spec: Dict, worktree: Path, target: Path, env: dict, timeout: float, log: List[str],
                      runner: Optional[str]) -> bool:
    venv = target / ".venv"
    py = sys.executable or "python3"
    if not (venv / "bin" / "python").exists() and not (venv / "Scripts" / "python.exe").exists():
        if _run([py, "-m", "venv", str(venv)], worktree, env, timeout, log) != 0:
            return False
    vpy = str(venv / "bin" / "python") if (venv / "bin" / "python").exists() else str(venv / "Scripts" / "python.exe")
    ok = True
    if spec["manager"] == "uv":
        env = dict(env, UV_PROJECT_ENVIRONMENT=str(venv))
        ok = _run(spec["install"], worktree, env, timeout, log) == 0
    elif spec["manager"] == "poetry":
        # Poetry uses an existing .venv in the project; link the cached one in first.
        link = worktree / ".venv"
        if not link.exists():
            os.symlink(venv, link, target_is_directory=True)
        ok = _run(spec["install"], worktree, dict(env, POETRY_VIRTUALENVS_IN_PROJECT="true"), timeout, log) == 0
    else:
        reqs = [n for n in spec["lock_files"] if n.endswith(".txt")]
        _run([vpy, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], worktree, env, timeout, log)
        for r in reqs:
            if _run([vpy, "-m", "pip", "install", "--quiet", "-r", r], worktree, env, timeout, log) != 0:
                ok = False
        if any(n in spec["lock_files"] for n in ("pyproject.toml", "setup.py", "setup.cfg")):
            # Try dev/test extras first; fall back to the bare project. Failures here are not fatal:
            # many repos are not installable packages, only their requirements matter.
            if _run([vpy, "-m", "pip", "install", "--quiet", "-e", ".[dev,test,tests]"], worktree, env, timeout, log) != 0:
                _run([vpy, "-m", "pip", "install", "--quiet", "-e", "."], worktree, env, timeout, log)
    # The verifier needs pytest inside the venv. Install it, or fall back to the system copy.
    if (runner or "pytest") == "pytest" and _run([vpy, "-c", "import pytest"], worktree, env, 60, log) != 0:
        if _run([vpy, "-m", "pip", "install", "--quiet", "pytest"], worktree, env, timeout, log) != 0:
            cfg = venv / "pyvenv.cfg"
            if cfg.exists():
                text = cfg.read_text()
                text = text.replace("include-system-site-packages = false", "include-system-site-packages = true")
                cfg.write_text(text)
                log.append("enabled system site-packages so the system pytest is visible")
    return ok


def _provision_dir_manager(spec: Dict, worktree: Path, target: Path, env: dict, timeout: float, log: List[str]) -> bool:
    """node_modules / vendor style managers: install in the worktree, move the result into the cache."""
    link = spec["link"]
    dest = target / link
    if (worktree / link).is_symlink():
        os.unlink(worktree / link)
    rc = _run(spec["install"], worktree, env, timeout, log)
    produced = worktree / link
    if rc != 0 or not produced.exists():
        return False
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(produced), str(dest))
    return True


def ensure(deps: Optional[Dict], worktree: Path, store_root: Path, repo_id: str, timeout: float = 1800,
           setup_cmd: Optional[str] = None, runner: Optional[str] = None, retry_failed: bool = False) -> Dict:
    """Provision (or reuse) dependencies for `worktree`. Returns a status dict for observations/info."""
    status: Dict = {"provisioned": [], "linked": [], "failed": [], "skipped": []}
    if setup_cmd:
        log: List[str] = []
        rc = _run(setup_cmd, worktree, dict(os.environ), timeout, log)
        (status["provisioned"] if rc == 0 else status["failed"]).append("setup_cmd")
        status["setup_cmd_log"] = "\n".join(log)[-4000:]
    if not deps:
        return status
    for spec in deps.get("specs", []):
        link = spec.get("link")
        if link and (worktree / link).exists():
            status["skipped"].append(f"{spec['manager']} ({link} already present)")
            continue
        if not link:
            # Global caches (go, cargo): just warm them, quietly.
            log = []
            rc = _run(spec["install"], worktree, dict(os.environ), timeout, log)
            (status["provisioned"] if rc == 0 else status["failed"]).append(spec["manager"])
            continue
        target = cache_root(store_root, repo_id) / f"{spec['manager']}-{spec['lock_hash']}"
        target.mkdir(parents=True, exist_ok=True)
        with file_lock(target / ".lock", timeout=timeout + 60):
            if (target / FAILED).exists() and not retry_failed:
                status["failed"].append(f"{spec['manager']} (previous attempt failed; see {target / FAILED})")
                continue
            if not (target / READY).exists():
                log = []
                env = dict(os.environ, CI="1", PIP_DISABLE_PIP_VERSION_CHECK="1", npm_config_update_notifier="false")
                if spec.get("kind") == "python":
                    ok = _provision_python(spec, worktree, target, env, timeout, log, runner or deps.get("runner"))
                else:
                    ok = _provision_dir_manager(spec, worktree, target, env, timeout, log)
                if ok:
                    write_json(target / READY, {"spec": spec, "at": now_iso()})
                    (target / FAILED).unlink(missing_ok=True)
                    status["provisioned"].append(spec["manager"])
                else:
                    (target / FAILED).write_text("\n".join(log)[-20000:], encoding="utf-8")
                    status["failed"].append(spec["manager"])
                    continue
        cached = target / link
        dest = worktree / link
        if cached.exists() and not dest.exists():
            os.symlink(cached, dest, target_is_directory=True)
            status["linked"].append(link)
    return status


def warm(task: dict, repo: Path, store_root: Path, cfg: dict) -> Dict:
    """Pre-provision a task's dependencies (e.g. before a training run) using a throwaway worktree."""
    from . import gitsnap
    from .verify import materialize
    with materialize(task, repo, cfg, provision=False) as wt:
        return ensure(task.get("deps"), wt, store_root, (task.get("repo") or {}).get("id") or "repo",
                      timeout=cfg.get("deps", {}).get("timeout", 1800), setup_cmd=task.get("setup_cmd") or cfg.get("setup_cmd"),
                      runner=task.get("runner"))
