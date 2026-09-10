"""Storage controls: large-file cap, snapshot pruning, disk accounting, and that pruned tasks still run."""
import subprocess

from repogym import builder, config, gitsnap
from repogym.env import RepoGymEnv
from repogym.gc import disk_usage, prune_snapshots
from repogym.store import Store
from tests.conftest import hook
from tests.test_end_to_end import run_session


def _objects_size(repo):
    out = subprocess.run(["git", "count-objects", "-v"], cwd=repo, capture_output=True, text=True).stdout
    return int([l for l in out.splitlines() if l.startswith("size:")][0].split()[1])  # KiB


def test_large_untracked_files_are_left_out_of_snapshots(pyrepo):
    (pyrepo / "model.bin").write_bytes(b"\0" * (6 * 1024 * 1024))
    (pyrepo / "notes.txt").write_text("small\n")
    before = _objects_size(pyrepo)
    snap = gitsnap.snapshot(pyrepo, "cap", max_file_size_mb=5)
    assert snap.skipped_large == ["model.bin"]
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", snap.commit], cwd=pyrepo,
                           capture_output=True, text=True).stdout.split()
    assert "notes.txt" in files and "model.bin" not in files
    assert _objects_size(pyrepo) - before < 1024  # well under 1 MiB added
    # No cap => everything goes in (opt-in behaviour).
    snap2 = gitsnap.snapshot(pyrepo, "nocap", max_file_size_mb=0)
    assert snap2.skipped_large == []


def test_capture_uses_configured_cap(home, pyrepo):
    (pyrepo / "dump.sql").write_bytes(b"x" * (6 * 1024 * 1024))
    ep = hook({"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": str(pyrepo), "prompt": "p"})
    assert ep["base"]["skipped_large"] == ["dump.sql"]


def test_prune_unpins_and_tasks_still_work(home, pyrepo):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, config.load_config(), quiet=True)
    task = store.tasks()[0]
    assert len(gitsnap.snapshot_refs(pyrepo)) == 2
    du = disk_usage(store)
    assert du["repos"][0]["snapshot_refs"] == 2 and du["repos"][0]["snapshot_bytes"] > 0
    assert du["tasks_bytes"] < 200 * 1024  # a task is tens of KB

    assert prune_snapshots(store, days=30) == []          # too young
    removed = prune_snapshots(store, days=0)               # retention 0 => unpin now
    assert len(removed) == 2 and gitsnap.snapshot_refs(pyrepo) == []
    assert store.find_episode(task["source"]["episode_id"]).meta["snapshots_pruned"] is True
    assert prune_snapshots(store, days=0) == []           # idempotent

    # Reclaim immediately, then prove the task is reproducible from head_commit + base.patch.
    gitsnap.garbage_collect(pyrepo, prune_now=True)
    assert not gitsnap.commit_exists(pyrepo, task["base_commit"])
    env = RepoGymEnv(task["id"], store=store)
    obs, _ = env.reset()
    assert "raise ValueError" not in (env.repo_path / "calc/core.py").read_text()
    env.step({"type": "patch", "diff": env.gold_source_patch})
    _, reward, terminated, _, info = env.step({"type": "submit"})
    assert terminated and reward == 1.0
    env.close()


def test_drain_runs_housekeeping_with_zero_retention(home, pyrepo):
    from repogym import config as cfgmod
    cfg = cfgmod.load_config()
    cfg["snapshot_retention_days"] = 0
    cfgmod.save_config(cfg)
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, cfgmod.load_config(), quiet=True)
    assert gitsnap.snapshot_refs(pyrepo) == []
    assert len(store.tasks()) == 1
