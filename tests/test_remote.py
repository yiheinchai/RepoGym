"""Shared object-store layer: push/pull/index, auto-sync with local purge, S3 key layout, and a
'training box' that only has the git remote and the bucket."""
import json
import subprocess
from pathlib import Path

import pytest

from repogym import builder, config
from repogym.env import RepoGymEnv
from repogym.remote import DirRemote, S3Remote, iter_tasks, is_synced, pull_task, split_task_url, sync_all
from repogym.store import Store
from tests.conftest import _git
from tests.test_end_to_end import run_session


def build_one(home, pyrepo):
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, config.load_config(), quiet=True)
    task = store.tasks()[0]
    return store, task


def test_split_task_url():
    assert split_task_url("s3://b/p/tasks/abc") == ("s3://b/p", "abc")
    assert split_task_url("file:///x/tasks/abc/") == ("file:///x", "abc")
    assert split_task_url("s3://b/p/tasks/abc/task.json") is None
    assert split_task_url("abc") is None


def test_dir_remote_roundtrip_and_purge(home, pyrepo, tmp_path):
    store, task = build_one(home, pyrepo)
    remote = DirRemote(f"file://{tmp_path / 'bucket'}")
    n = sync_all(store, remote, purge_local=True)
    assert n == 1
    assert not store.task_dir(task["id"]).exists()           # local copy gone
    assert is_synced(store.root, task["id"])
    assert remote.list_task_ids() == [task["id"]]
    files = {k.rsplit("/", 1)[-1] for k in remote.list_files(remote.task_key(task["id"]))}
    assert {"task.json", "problem.md", "solution.patch", "source.patch", "test.patch", "base.patch",
            "trajectory.jsonl", "snapshots.bundle"} <= files
    assert sync_all(store, remote) == 0                        # nothing left to push
    assert remote.rebuild_index() == 1
    metas = list(iter_tasks(remote.url, tiers=["verified"]))
    assert metas[0]["id"] == task["id"] and metas[0]["remote_url"].endswith(f"/tasks/{task['id']}")
    # Pull into the cache and run the env from a remote task URL.
    env = RepoGymEnv(f"{remote.url}/tasks/{task['id']}", store=store)
    env.reset()
    env.step({"type": "patch", "diff": env.gold_source_patch})
    _, reward, _, _, info = env.step({"type": "submit"})
    assert reward == 1.0
    env.close()
    assert (store.root / "cache" / "tasks" / task["id"] / "task.json").exists()


def test_auto_sync_after_build(home, pyrepo, tmp_path):
    cfg = config.load_config()
    cfg["remote"].update({"url": f"file://{tmp_path / 'bucket'}", "auto_sync": True, "purge_local": True})
    config.save_config(cfg)
    run_session(pyrepo)
    store = Store(home)
    builder.drain_queue(store, config.load_config(), quiet=True)
    assert store.tasks() == []                                  # purged locally...
    remote = DirRemote(cfg["remote"]["url"])
    ids = remote.list_task_ids()
    assert len(ids) == 1                                        # ...but in the bucket
    # A bare task id resolves through the configured remote.
    env = RepoGymEnv(ids[0], store=store)
    obs, _ = env.reset()
    assert obs["problem_statement"].startswith("divide()")
    env.close()


def test_training_box_clones_from_git_remote(home, pyrepo, tmp_path):
    # The engineer's repo has an origin; the "training box" only has the bucket and that origin.
    origin = tmp_path / "origin.git"
    _git(pyrepo, "clone", "--bare", "-q", str(pyrepo), str(origin))
    _git(pyrepo, "remote", "add", "origin", str(origin))
    (pyrepo / "README.md").write_text("# calc\n\nuncommitted WIP\n")   # dirty base => base != head
    store, task = build_one(home, pyrepo)
    assert task["base_dirty"] and task["repo"]["remote"] == str(origin)
    remote = DirRemote(f"file://{tmp_path / 'bucket'}")
    sync_all(store, remote, purge_local=True)

    # Hide the engineer's checkout: rewrite the pulled task to point at a path that does not exist.
    tdir = pull_task(store, task["id"], remote)
    meta = json.loads((tdir / "task.json").read_text())
    meta["repo"]["path"] = "/nonexistent/checkout"
    (tdir / "task.json").write_text(json.dumps(meta))

    env = RepoGymEnv(task["id"], store=store)
    assert env.repo == store.root / "clones" / "calc"
    # snapshots.bundle restored the exact base commit inside the clone.
    from repogym import gitsnap
    assert gitsnap.commit_exists(env.repo, task["base_commit"])
    obs, _ = env.reset()
    assert "uncommitted WIP" in (env.repo_path / "README.md").read_text()
    assert "raise ValueError" not in (env.repo_path / "calc/core.py").read_text()
    env.step({"type": "patch", "diff": env.gold_source_patch})
    _, reward, _, _, _ = env.step({"type": "submit"})
    assert reward == 1.0
    env.close()


class FakeS3:
    """Just enough of boto3's S3 client for S3Remote."""

    def __init__(self):
        self.objects = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.last_extra = ExtraArgs

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            e = Exception("NoSuchKey")
            e.response = {"Error": {"Code": "NoSuchKey"}}
            raise e
        import io
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter):
                contents, prefixes = [], set()
                for (b, k) in fake.objects:
                    if b != Bucket or not k.startswith(Prefix):
                        continue
                    rest = k[len(Prefix):]
                    if Delimiter in rest:
                        prefixes.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
                    else:
                        contents.append({"Key": k})
                yield {"Contents": contents, "CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)]}
        return P()


def test_s3_remote_layout_with_fake_client(home, pyrepo, tmp_path):
    store, task = build_one(home, pyrepo)
    fake = FakeS3()
    remote = S3Remote("s3://acme-gym/prod/", client=fake, extra_args={"ServerSideEncryption": "AES256"})
    remote.push_task(store.task_dir(task["id"]))
    keys = sorted(k for (_b, k) in fake.objects)
    assert keys[0].startswith(f"prod/tasks/{task['id']}/")
    assert f"prod/tasks/{task['id']}/task.json" in keys
    assert fake.last_extra == {"ServerSideEncryption": "AES256"}
    assert remote.list_task_ids() == [task["id"]]
    assert remote.has_task(task["id"]) and not remote.has_task("nope")
    assert remote.get_task_meta(task["id"])["id"] == task["id"]
    assert remote.rebuild_index() == 1 and remote.read_index()[0]["id"] == task["id"]
    dest = remote.pull_task(task["id"], tmp_path / "pulled")
    assert (dest / "solution.patch").read_text() == (store.task_dir(task["id"]) / "solution.patch").read_text()
    remote.delete_task(task["id"])
    assert remote.list_task_ids() == []
    assert ("acme-gym", "prod/index.jsonl") in fake.objects  # index untouched by task deletion
