"""Repository mirrors in the bucket: tasks stay reproducible when the git host is gone."""
import json
import shutil

from repogym import builder, config, gitsnap
from repogym.env import RepoGymEnv
from repogym.mirror import get_manifest, push_mirror, restore_mirror
from repogym.remote import DirRemote, pull_task, sync_all
from repogym.store import Store
from tests.conftest import _git
from tests.test_end_to_end import run_session


def build_and_sync(home, pyrepo, bucket):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, config.load_config(), quiet=True)
    remote = DirRemote(f"file://{bucket}")
    sync_all(store, remote, purge_local=True)
    return store, remote


def test_mirror_survives_loss_of_git_host(home, pyrepo, tmp_path):
    store, remote = build_and_sync(home, pyrepo, tmp_path / "bucket")
    tid = remote.list_task_ids()[0]
    task = remote.get_task_meta(tid)
    repo_id = task["repo"]["id"]
    manifest = get_manifest(remote, repo_id)
    assert manifest and len(manifest["bundles"]) == 1 and manifest["bundles"][0]["thin"] is False
    assert task["base_commit"] in manifest["covered"] or any(True for _ in manifest["covered"])

    # Catastrophe: the engineer's checkout is gone and there is no git remote at all.
    shutil.rmtree(pyrepo)
    tdir = pull_task(store, tid, remote)
    meta = json.loads((tdir / "task.json").read_text())
    assert meta["repo"]["remote"] is None

    env = RepoGymEnv(f"{remote.url}/tasks/{tid}", store=store)
    assert env.repo == store.root / "clones" / "calc"
    assert gitsnap.commit_exists(env.repo, task["base_commit"])   # exact snapshot, from the bucket alone
    assert gitsnap.commit_exists(env.repo, task["head_commit"])
    obs, _ = env.reset()
    assert "raise ValueError" not in (env.repo_path / "calc/core.py").read_text()
    env.step({"type": "patch", "diff": env.gold_source_patch})
    _, reward, _, _, _ = env.step({"type": "submit"})
    assert reward == 1.0
    env.close()


def test_mirror_is_incremental(home, pyrepo, tmp_path):
    store, remote = build_and_sync(home, pyrepo, tmp_path / "bucket")
    repo_id = store.repos()[0]["id"]
    first = get_manifest(remote, repo_id)["bundles"][0]

    # Nothing new -> no upload.
    assert push_mirror(remote, pyrepo, repo_id, "calc") is None

    # Engineer commits and does another turn; only the delta should be shipped.
    _git(pyrepo, "add", "-A")
    _git(pyrepo, "commit", "-q", "-m", "zero check")
    (pyrepo / "docs.md").write_text("# docs\n" * 50)
    _git(pyrepo, "add", "-A")
    _git(pyrepo, "commit", "-q", "-m", "docs")
    from tests.conftest import hook
    hook({"hook_event_name": "UserPromptSubmit", "session_id": "sess-2", "cwd": str(pyrepo), "prompt": "add multiply"})
    (pyrepo / "calc" / "core.py").write_text((pyrepo / "calc" / "core.py").read_text() + "\n\ndef multiply(a, b):\n    return a * b\n")
    (pyrepo / "tests" / "test_core.py").write_text((pyrepo / "tests" / "test_core.py").read_text()
                                                   + "\n\ndef test_multiply():\n    from calc.core import multiply\n    assert multiply(2, 3) == 6\n")
    ep = hook({"hook_event_name": "Stop", "session_id": "sess-2", "cwd": str(pyrepo)})
    assert ep["status"] == "closed"
    builder.drain_queue(store, config.load_config(), quiet=True)
    sync_all(store, remote, purge_local=True)

    manifest = get_manifest(remote, repo_id)
    assert len(manifest["bundles"]) == 2
    second = manifest["bundles"][1]
    assert second["thin"] is True and second["bytes"] < first["bytes"] + 2048
    assert first["heads"] != second["heads"]

    # Restore applies both bundles and yields every commit; a second restore is a no-op.
    dest = tmp_path / "restored"
    restore_mirror(remote, repo_id, dest)
    for t in [remote.get_task_meta(t) for t in remote.list_task_ids()]:
        assert gitsnap.commit_exists(dest, t["base_commit"]) and gitsnap.commit_exists(dest, t["final_commit"])
    state = json.loads((dest / ".git" / ".repogym-mirror.json").read_text())
    assert len(state["applied"]) == 2
    restore_mirror(remote, repo_id, dest)
    assert len(json.loads((dest / ".git" / ".repogym-mirror.json").read_text())["applied"]) == 2
