import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "repogym-home"
    monkeypatch.setenv("REPOGYM_HOME", str(h))
    monkeypatch.setenv("REPOGYM_NO_SPAWN", "1")
    monkeypatch.delenv("REPOGYM_OPT_OUT", raising=False)
    from repogym import config
    config.ensure_home()
    return h


@pytest.fixture
def pyrepo(tmp_path):
    """A tiny Python project with a pytest suite and one bug (calc.divide has no zero check)."""
    repo = tmp_path / "calc"
    (repo / "calc").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "calc" / "__init__.py").write_text("from .core import add, divide\n")
    (repo / "calc" / "core.py").write_text(textwrap.dedent('''
        def add(a, b):
            return a + b


        def divide(a, b):
            return a / b
        '''))
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_core.py").write_text(textwrap.dedent('''
        from calc import add, divide


        def test_add():
            assert add(1, 2) == 3


        def test_divide():
            assert divide(6, 3) == 2
        '''))
    (repo / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n")
    (repo / "README.md").write_text("# calc\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def hook(payload: dict):
    """Invoke the Claude Code hook handler in-process (same code path as `repogym hook`)."""
    from repogym.adapters.claude_code import handle
    return handle(payload)


def apply_fix(repo: Path):
    """The 'agent' edits: add a zero check and a regression test."""
    (repo / "calc" / "core.py").write_text(textwrap.dedent('''
        def add(a, b):
            return a + b


        def divide(a, b):
            if b == 0:
                raise ValueError("division by zero")
            return a / b
        '''))
    (repo / "tests" / "test_core.py").write_text(textwrap.dedent('''
        import pytest

        from calc import add, divide


        def test_add():
            assert add(1, 2) == 3


        def test_divide():
            assert divide(6, 3) == 2


        def test_divide_by_zero_raises():
            with pytest.raises(ValueError):
                divide(1, 0)
        '''))
