import json

from repogym.adapters import claude_code, codex
from repogym.store import Store
from tests.conftest import apply_fix


def test_install_and_uninstall_claude_hooks(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    claude_code.install(path, command="repogym hook")
    s = json.loads(path.read_text())
    assert s["permissions"] == {"allow": ["Bash(ls)"]}
    assert len(s["hooks"]["Stop"]) == 2  # user's own hook preserved
    assert any(h["command"] == "repogym hook" for g in s["hooks"]["Stop"] for h in g["hooks"])
    assert s["hooks"]["PostToolUse"][0]["matcher"] == "Bash|Edit|Write|MultiEdit|NotebookEdit"
    assert "matcher" not in s["hooks"]["UserPromptSubmit"][0]
    # idempotent
    claude_code.install(path, command="repogym hook")
    s2 = json.loads(path.read_text())
    assert len(s2["hooks"]["Stop"]) == 2
    claude_code.uninstall(path)
    s3 = json.loads(path.read_text())
    assert s3["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}


def test_codex_notify_install(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('model = "o3"\n\n[mcp_servers.foo]\ncommand = "x"\n')
    codex.install_notify(p)
    text = p.read_text()
    assert text.startswith('notify = ["repogym", "codex-notify"]\n')
    assert text.index("notify") < text.index("[mcp_servers")
    codex.install_notify(p)
    assert text == p.read_text()  # idempotent
    codex.uninstall_notify(p)
    assert "notify" not in p.read_text()


def test_codex_turns_chain(home, pyrepo):
    payload = {"type": "agent-turn-complete", "thread-id": "t1", "cwd": str(pyrepo),
               "input-messages": ["fix divide by zero"], "last-assistant-message": "done"}
    apply_fix(pyrepo)
    ep1 = codex.handle_notify(payload)
    assert ep1["agent"] == "codex" and ep1["meta"]["base_uncertain"] is True
    # Codex first turn: base snapshot happened at turn end -> no change -> skipped, but a fresh
    # episode is opened so the next turn is precise.
    assert ep1["status"] == "skipped"
    store = Store(home)
    assert store.open_episode(ep1["repo_id"], "t1") is not None
    (pyrepo / "calc" / "core.py").write_text((pyrepo / "calc" / "core.py").read_text() + "\nX = 1\n")
    ep2 = codex.handle_notify(dict(payload, **{"input-messages": ["add X"]}))
    assert ep2["status"] == "closed" and ep2["prompts"] == ["add X"]
    assert ep2["base"]["tree"] == ep1["final"]["tree"]
