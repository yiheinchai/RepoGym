"""Dependency provisioning: detection, the per-lockfile cache, and an end-to-end Python repo whose
tests need a third-party package that only exists as a wheel inside the repo (no network)."""
import io
import json
import os
import shutil
import textwrap
import zipfile
from pathlib import Path

import pytest

from repogym import builder, config, deps
from repogym.env import RepoGymEnv
from repogym.store import Store
from tests.conftest import _git, hook


def test_detect_node_and_python(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    spec = deps.detect(tmp_path, runner="pytest")
    managers = {s["manager"]: s for s in spec["specs"]}
    assert managers["pnpm"]["install"] == ["pnpm", "install", "--frozen-lockfile"]
    assert managers["pnpm"]["link"] == "node_modules"
    assert managers["pip"]["link"] == ".venv" and "requirements.txt" in managers["pip"]["lock_files"]
    h1 = managers["pip"]["lock_hash"]
    (tmp_path / "requirements.txt").write_text("requests==2.32.1\n")
    h2 = {s["manager"]: s for s in deps.detect(tmp_path)["specs"]}["pip"]["lock_hash"]
    assert h1 != h2                                            # cache key follows the lockfile
    assert deps.detect(tmp_path / "nowhere") is None or deps.detect(tmp_path.parent / "empty_dir_x") is None


def test_detect_prefers_lockfile_specific_installers(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    assert deps.detect(tmp_path)["specs"][0]["install"][:2] == ["npm", "ci"]
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    assert {s["manager"] for s in deps.detect(tmp_path)["specs"]} == {"npm", "uv"}
    (tmp_path / "go.mod").write_text("module x\n")
    assert any(s["manager"] == "go" and s["link"] is None for s in deps.detect(tmp_path)["specs"])


def test_cache_installs_once_and_links_everywhere(tmp_path):
    counter = tmp_path / "installs.log"
    spec = {"specs": [{"manager": "stub", "kind": "node", "link": "node_modules", "lock_hash": "abc123",
                       "lock_files": [], "install": ["sh", "-c", f"mkdir node_modules && echo hi > node_modules/m.txt && echo run >> {counter}"]}]}
    home = tmp_path / "home"
    wt1, wt2 = tmp_path / "wt1", tmp_path / "wt2"
    wt1.mkdir(); wt2.mkdir()
    s1 = deps.ensure(spec, wt1, home, "repoA")
    assert s1["provisioned"] == ["stub"] and s1["linked"] == ["node_modules"]
    assert (wt1 / "node_modules").is_symlink() and (wt1 / "node_modules" / "m.txt").read_text() == "hi\n"
    s2 = deps.ensure(spec, wt2, home, "repoA")
    assert s2["provisioned"] == [] and s2["linked"] == ["node_modules"]   # reused, not reinstalled
    assert counter.read_text().count("run") == 1
    cache = home / "deps" / "repoA" / "stub-abc123"
    assert (cache / ".ready").exists() and (cache / "node_modules" / "m.txt").exists()
    # A worktree that already has the directory (engineer's machine) is left alone.
    wt3 = tmp_path / "wt3"; (wt3 / "node_modules").mkdir(parents=True)
    assert deps.ensure(spec, wt3, home, "repoA")["skipped"]


def test_failed_install_is_recorded_not_retried(tmp_path):
    spec = {"specs": [{"manager": "bad", "kind": "node", "link": "node_modules", "lock_hash": "h", "lock_files": [],
                       "install": ["sh", "-c", "echo boom >&2; exit 3"]}]}
    wt = tmp_path / "wt"; wt.mkdir()
    s = deps.ensure(spec, wt, tmp_path / "home", "r")
    assert s["failed"] == ["bad"]
    assert "boom" in (tmp_path / "home" / "deps" / "r" / "bad-h" / ".failed").read_text()
    s = deps.ensure(spec, wt, tmp_path / "home", "r")
    assert "previous attempt failed" in s["failed"][0]


def _make_wheel(dest: Path, name: str = "deplib", version: str = "0.1") -> Path:
    """Hand-built pure-Python wheel so the test needs no PyPI and no build backend."""
    dest.mkdir(parents=True, exist_ok=True)
    whl = dest / f"{name}-{version}-py3-none-any.whl"
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "def shout(s):\n    return s.upper() + '!'\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: repogym-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/top_level.txt": f"{name}\n",
    }
    record = "".join(f"{k},,\n" for k in files) + f"{dist}/RECORD,,\n"
    with zipfile.ZipFile(whl, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
        z.writestr(f"{dist}/RECORD", record)
    return whl


@pytest.fixture
def pyrepo_with_deps(pyrepo, monkeypatch):
    monkeypatch.setenv("PIP_NO_INDEX", "1")          # no network in tests; pytest falls back to system copy
    _make_wheel(pyrepo / "wheels")
    (pyrepo / "requirements.txt").write_text("./wheels/deplib-0.1-py3-none-any.whl\n")
    _git(pyrepo, "add", "-A")
    _git(pyrepo, "commit", "-q", "-m", "add deplib dependency")
    return pyrepo


def test_python_task_with_third_party_dependency(home, pyrepo_with_deps):
    repo = pyrepo_with_deps
    cwd = str(repo)
    hook({"hook_event_name": "UserPromptSubmit", "session_id": "d", "cwd": cwd,
          "prompt": "add greet(name) that uses deplib.shout"})
    (repo / "calc" / "core.py").write_text((repo / "calc" / "core.py").read_text() + textwrap.dedent('''

        def greet(name):
            from deplib import shout
            return shout("hi " + name)
        '''))
    (repo / "tests" / "test_core.py").write_text((repo / "tests" / "test_core.py").read_text() + textwrap.dedent('''

        def test_greet_uses_deplib():
            from calc.core import greet
            assert greet("bob") == "HI BOB!"
        '''))
    hook({"hook_event_name": "PostToolUse", "session_id": "d", "cwd": cwd, "tool_name": "Bash",
          "tool_input": {"command": "pytest -q"}})
    hook({"hook_event_name": "Stop", "session_id": "d", "cwd": cwd})
    store = Store(home)
    builder.drain_queue(store, config.load_config(), quiet=True)
    task = store.tasks()[0]
    # The builder provisioned a venv with deplib (from the wheel) and verified with it.
    assert task["deps"]["specs"][0]["manager"] == "pip"
    assert task["tier"] == "verified", task["verification"]
    assert task["FAIL_TO_PASS"] == ["tests/test_core.py::test_greet_uses_deplib"]
    cache = home / "deps" / task["repo"]["id"]
    venvs = list(cache.glob("pip-*/.venv"))
    assert len(venvs) == 1

    # Training box: cold cache, no engineer dirs. reset() provisions; the agent's shell has the venv.
    shutil.rmtree(cache)
    env = RepoGymEnv(task["id"], store=store)
    obs, info = env.reset()
    assert obs["deps_ready"] is True and info["deps"]["provisioned"] == ["pip"]
    assert (env.repo_path / ".venv").is_symlink()
    obs, *_ = env.step("python -c 'import deplib, sys; print(deplib.shout(\"ok\"), sys.prefix)'")
    assert "OK!" in obs["last_output"] and ".venv" in obs["last_output"]
    obs, *_ = env.step("python -m pytest -q")
    assert "[exit 0]" in obs["last_output"]                      # visible tests pass at base
    env.step({"type": "patch", "diff": env.gold_source_patch})
    _, reward, _, _, _ = env.step({"type": "submit"})
    assert reward == 1.0
    env.close()
    # Second episode reuses the cache: nothing reinstalled.
    env2 = RepoGymEnv(task["id"], store=store)
    _, info2 = env2.reset()
    assert info2["deps"]["provisioned"] == [] and info2["deps"]["linked"] == [".venv"]
    env2.close()
