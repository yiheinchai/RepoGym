"""End-to-end on a Go module, exercising `go test -json` parsing. Skipped when go is absent."""
import shutil
import subprocess
import textwrap

import pytest

from repogym import builder, config
from repogym.store import Store
from tests.conftest import _git, hook

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")


@pytest.fixture
def gorepo(tmp_path):
    repo = tmp_path / "gomod"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/gomod\n\ngo 1.21\n")
    (repo / "mathx.go").write_text(textwrap.dedent('''
        package gomod

        func Abs(x int) int {
            return x
        }
        '''))
    (repo / "mathx_test.go").write_text(textwrap.dedent('''
        package gomod

        import "testing"

        func TestAbsPositive(t *testing.T) {
            if Abs(3) != 3 {
                t.Fatal("bad")
            }
        }
        '''))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_go_fail_to_pass(home, gorepo):
    cwd = str(gorepo)
    hook({"hook_event_name": "UserPromptSubmit", "session_id": "g", "cwd": cwd, "prompt": "Abs must handle negatives"})
    (gorepo / "mathx.go").write_text(textwrap.dedent('''
        package gomod

        func Abs(x int) int {
            if x < 0 {
                return -x
            }
            return x
        }
        '''))
    (gorepo / "mathx_test.go").write_text((gorepo / "mathx_test.go").read_text() + textwrap.dedent('''

        func TestAbsNegative(t *testing.T) {
            if Abs(-3) != 3 {
                t.Fatal("bad")
            }
        }
        '''))
    hook({"hook_event_name": "PostToolUse", "session_id": "g", "cwd": cwd, "tool_name": "Bash",
          "tool_input": {"command": "go test ./..."}})
    hook({"hook_event_name": "Stop", "session_id": "g", "cwd": cwd})
    store = Store(home)
    cfg = dict(config.load_config(), test_timeout=600)
    builder.drain_queue(store, cfg, quiet=True)
    tasks = store.tasks()
    assert len(tasks) == 1, [e.note for e in store.episodes()]
    t = tasks[0]
    assert t["runner"] == "go" and t["language"] == "go"
    assert t["tier"] == "verified", t["verification"]
    assert t["FAIL_TO_PASS"] == ["example.com/gomod/TestAbsNegative"]
    assert t["PASS_TO_PASS"] == ["example.com/gomod/TestAbsPositive"]
