"""Simulate a Claude Code session on the fixture repo and check the whole pipeline."""
import json
import subprocess
import textwrap

import pytest

from repogym import builder, config
from repogym.env import RepoGymEnv
from repogym.export import export
from repogym.store import Store
from repogym.verify import TIER_VERIFIED, verify_patch
from tests.conftest import apply_fix, hook


def run_session(pyrepo, session="sess-1", prompt="divide() should raise ValueError on division by zero instead of ZeroDivisionError. Add a test."):
    hook({"hook_event_name": "SessionStart", "session_id": session, "cwd": str(pyrepo), "source": "startup"})
    hook({"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(pyrepo), "prompt": prompt})
    hook({"hook_event_name": "PostToolUse", "session_id": session, "cwd": str(pyrepo), "tool_name": "Read",
          "tool_input": {"file_path": str(pyrepo / "calc/core.py")}})
    apply_fix(pyrepo)
    hook({"hook_event_name": "PostToolUse", "session_id": session, "cwd": str(pyrepo), "tool_name": "Edit",
          "tool_input": {"file_path": str(pyrepo / "calc/core.py"), "old_string": "x", "new_string": "y"}})
    hook({"hook_event_name": "PostToolUse", "session_id": session, "cwd": str(pyrepo), "tool_name": "Bash",
          "tool_input": {"command": "python -m pytest tests/test_core.py -q  # token=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"},
          "tool_response": {"exit_code": 0}})
    return hook({"hook_event_name": "Stop", "session_id": session, "cwd": str(pyrepo), "stop_hook_active": False})


def test_capture_creates_closed_episode(home, pyrepo):
    ep = run_session(pyrepo)
    assert ep is not None
    assert ep["status"] == "closed"
    assert ep["base"]["dirty"] is False
    assert ep["final"]["tree"] != ep["base"]["tree"]
    assert ep["prompts"][0].startswith("divide() should raise")
    tools = [t["tool"] for t in ep["tools"]]
    assert tools == ["Read", "Edit", "Bash"]
    assert ep["test_commands"] and "sk-ant" not in ep["test_commands"][0]  # scrubbed
    assert "[REDACTED]" in ep["tools"][2]["command"]
    store = Store(home)
    assert store.queue_size() == 1


def test_no_change_turn_is_skipped(home, pyrepo):
    hook({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": str(pyrepo), "prompt": "explain this repo"})
    ep = hook({"hook_event_name": "Stop", "session_id": "s", "cwd": str(pyrepo)})
    assert ep["status"] == "skipped" and ep["note"] == "no_changes"
    assert Store(home).queue_size() == 0


def test_opt_out_disables_capture(home, pyrepo, monkeypatch):
    (pyrepo / ".repogym-optout").write_text("")
    assert hook({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": str(pyrepo), "prompt": "x"}) is None
    (pyrepo / ".repogym-optout").unlink()
    monkeypatch.setenv("REPOGYM_OPT_OUT", "1")
    assert hook({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": str(pyrepo), "prompt": "x"}) is None


def test_build_discovers_fail_to_pass(home, pyrepo):
    run_session(pyrepo)
    store = Store(home)
    n = builder.drain_queue(store, config.load_config(), quiet=True)
    assert n == 1
    tasks = store.tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["tier"] == TIER_VERIFIED
    assert task["FAIL_TO_PASS"] == ["tests/test_core.py::test_divide_by_zero_raises"]
    assert set(task["PASS_TO_PASS"]) == {"tests/test_core.py::test_add", "tests/test_core.py::test_divide"}
    assert task["runner"] == "pytest"
    assert task["language"] == "python"
    assert task["test_files"] == ["tests/test_core.py"]
    assert task["source_files"] == ["calc/core.py"]
    tdir = store.task_dir(task["id"])
    assert "raise ValueError" in (tdir / "solution.patch").read_text()
    assert "raise ValueError" not in (tdir / "test.patch").read_text()
    assert "test_divide_by_zero_raises" in (tdir / "test.patch").read_text()
    assert (tdir / "problem.md").read_text().startswith("divide() should raise")
    traj = [json.loads(l) for l in (tdir / "trajectory.jsonl").read_text().splitlines()]
    assert len(traj) == 3
    ep = store.find_episode(task["source"]["episode_id"])
    assert ep.status == "built" and ep.task_id == task["id"]


def test_verify_gold_and_bad_patches(home, pyrepo):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, quiet=True)
    task = store.tasks()[0]
    tdir = store.task_dir(task["id"])
    gold = (tdir / "source.patch").read_text()
    info = verify_patch(task, tdir, gold, cfg=config.load_config())
    assert info["success"] is True and info["reward"] == 1.0
    # An empty patch: hidden test fails, F2P 0/1.
    info = verify_patch(task, tdir, "", cfg=config.load_config())
    assert info["success"] is False and info["reward"] == 0.0
    assert info["fail_to_pass_passed"] == 0
    # A patch that "fixes" the bug by breaking add(): P2P failure => zero reward even if F2P passes.
    bad = textwrap.dedent('''
        diff --git a/calc/core.py b/calc/core.py
        --- a/calc/core.py
        +++ b/calc/core.py
        @@ -1,7 +1,9 @@

         def add(a, b):
        -    return a + b
        +    return a - b


         def divide(a, b):
        +    if b == 0:
        +        raise ValueError("division by zero")
             return a / b
        ''').lstrip("\n")
    info = verify_patch(task, tdir, bad, cfg=config.load_config())
    assert info["fail_to_pass_passed"] == 1
    assert info["pass_to_pass_failed"] == ["tests/test_core.py::test_add"]
    assert info["success"] is False and info["reward"] == 0.0


def test_env_loop(home, pyrepo):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, quiet=True)
    task = store.tasks()[0]
    env = RepoGymEnv(task["id"], store=store)
    obs, info = env.reset()
    assert obs["problem_statement"].startswith("divide()")
    assert obs["repo_path"] and obs["repo_path"] != str(pyrepo)  # never the engineer's checkout
    # Hidden test is not visible before submit.
    obs, r, term, trunc, _ = env.step({"type": "read", "path": "tests/test_core.py"})
    assert "test_divide_by_zero_raises" not in obs["last_output"]
    obs, r, term, trunc, _ = env.step("python -m pytest -q")
    assert "[exit 0]" in obs["last_output"]
    obs, r, term, trunc, _ = env.step({"type": "write", "path": "calc/core.py", "content": textwrap.dedent('''
        def add(a, b):
            return a + b


        def divide(a, b):
            if b == 0:
                raise ValueError("nope")
            return a / b
        ''')})
    assert r == 0.0 and not term
    obs, reward, terminated, truncated, info = env.step({"type": "submit"})
    assert terminated and reward == 1.0 and info["success"] is True
    env.close()
    assert env.repo_path is None
    # The engineer's working tree was never modified by any of this.
    assert "raise ValueError" in (pyrepo / "calc/core.py").read_text()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=pyrepo, capture_output=True, text=True).stdout
    assert "tests/test_core.py" in status  # still the engineer's uncommitted edit, untouched


def test_env_failed_submit_gets_zero(home, pyrepo):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, quiet=True)
    env = RepoGymEnv(store.tasks()[0]["id"], store=store)
    env.reset()
    _, reward, terminated, _, info = env.step({"type": "submit"})
    assert terminated and reward == 0.0 and info["success"] is False
    env.close()


def test_export_swebench(home, pyrepo, tmp_path):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, quiet=True)
    out = tmp_path / "gym.jsonl"
    assert export(store, out, "swebench") == 1
    rec = json.loads(out.read_text().splitlines()[0])
    for key in ("instance_id", "repo", "base_commit", "patch", "test_patch", "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS"):
        assert key in rec
    assert json.loads(rec["FAIL_TO_PASS"]) == ["tests/test_core.py::test_divide_by_zero_raises"]
    assert "test_divide_by_zero_raises" not in rec["patch"]


def test_multi_turn_session_produces_separate_episodes_with_chained_bases(home, pyrepo):
    first = run_session(pyrepo)
    hook({"hook_event_name": "UserPromptSubmit", "session_id": "sess-1", "cwd": str(pyrepo), "prompt": "now add multiply"})
    (pyrepo / "calc" / "core.py").write_text((pyrepo / "calc" / "core.py").read_text() + "\n\ndef multiply(a, b):\n    return a * b\n")
    second = hook({"hook_event_name": "Stop", "session_id": "sess-1", "cwd": str(pyrepo)})
    assert second["id"] != first["id"]
    assert second["base"]["tree"] == first["final"]["tree"]
    store = Store(home)
    merged = builder.merge_session(store, first["repo_id"], "sess-1")
    assert merged is not None and merged.base["tree"] == first["base"]["tree"] and merged.final["tree"] == second["final"]["tree"]
    assert len(merged.prompts) == 2


def test_dirty_base_is_reconstructable_from_head_plus_base_patch(home, pyrepo):
    # Engineer had uncommitted work before asking the agent for help.
    (pyrepo / "README.md").write_text("# calc\n\nWIP notes\n")
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, quiet=True)
    task = store.tasks()[0]
    assert task["base_dirty"] is True
    base_patch = (store.task_dir(task["id"]) / "base.patch").read_text()
    assert "WIP notes" in base_patch
    # Simulate a machine that only has the real commits: delete the snapshot refs and gc-proof
    # reconstruction via head_commit + base.patch.
    from repogym import gitsnap
    from repogym.verify import materialize
    task2 = dict(task, base_commit="0" * 40)
    with materialize(task2, pyrepo, config.load_config(), task_dir=store.task_dir(task["id"])) as wt:
        assert "WIP notes" in (wt / "README.md").read_text()
        assert "raise ValueError" not in (wt / "calc/core.py").read_text()
