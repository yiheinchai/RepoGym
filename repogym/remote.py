"""Remote task storage: the organisation's gym lives in a bucket, not on laptops.

    repogym remote set s3://acme-repogym/gym        # once, per machine (or REPOGYM_REMOTE)
    repogym sync --purge-local                       # push built tasks, drop local copies
    repogym pull --all                               # training box: fetch everything
    RepoGymEnv("s3://acme-repogym/gym/tasks/<id>")   # or just use a task URL

Bucket layout (prefix = whatever follows the bucket):

    <prefix>/tasks/<task_id>/task.json | problem.md | solution.patch | source.patch | test.patch
                              | base.patch | trajectory.jsonl | snapshots.bundle
    <prefix>/index.jsonl      one task.json per line, rebuilt by `repogym remote index`

Backends: s3:// (boto3 if installed, else the aws CLI), gs:// (gcloud CLI), file:// or a plain
path (NFS / shared drive; also used in tests). Auth is whatever the SDK/CLI already uses.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlparse

from . import config
from .util import now_iso, read_json, write_json

TASK_FILES = ("task.json", "problem.md", "solution.patch", "source.patch", "test.patch", "base.patch",
              "trajectory.jsonl", "snapshots.bundle")


class RemoteError(RuntimeError):
    pass


class Remote:
    """Interface every backend implements. Keys are relative to the configured prefix."""

    url: str

    def put_file(self, local: Path, key: str) -> None: raise NotImplementedError
    def get_file(self, key: str, local: Path) -> None: raise NotImplementedError
    def get_text(self, key: str) -> Optional[str]: raise NotImplementedError
    def put_text(self, key: str, text: str) -> None: raise NotImplementedError
    def list_dirs(self, key_prefix: str) -> List[str]: raise NotImplementedError
    def list_files(self, key_prefix: str) -> List[str]: raise NotImplementedError
    def delete_prefix(self, key_prefix: str) -> None: raise NotImplementedError

    # ---- task-level helpers shared by all backends ---------------------------------------
    def task_key(self, task_id: str, name: str = "") -> str:
        return f"tasks/{task_id}/{name}" if name else f"tasks/{task_id}/"

    def push_task(self, task_dir: Path) -> List[str]:
        task = read_json(task_dir / "task.json")
        if not task:
            raise RemoteError(f"{task_dir} has no task.json")
        pushed = []
        for name in TASK_FILES:
            p = task_dir / name
            if p.exists():
                self.put_file(p, self.task_key(task["id"], name))
                pushed.append(name)
        return pushed

    def pull_task(self, task_id: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        files = self.list_files(self.task_key(task_id))
        if not files:
            raise RemoteError(f"task not found in remote: {task_id}")
        for key in files:
            self.get_file(key, dest / key.rsplit("/", 1)[-1])
        return dest

    def list_task_ids(self) -> List[str]:
        return sorted(self.list_dirs("tasks/"))

    def get_task_meta(self, task_id: str) -> Optional[dict]:
        text = self.get_text(self.task_key(task_id, "task.json"))
        return json.loads(text) if text else None

    def has_task(self, task_id: str) -> bool:
        return self.get_text(self.task_key(task_id, "task.json")) is not None

    def delete_task(self, task_id: str) -> None:
        self.delete_prefix(self.task_key(task_id))

    def rebuild_index(self) -> int:
        lines = []
        for tid in self.list_task_ids():
            meta = self.get_task_meta(tid)
            if meta:
                lines.append(json.dumps(meta, ensure_ascii=False))
        self.put_text("index.jsonl", "\n".join(lines) + ("\n" if lines else ""))
        return len(lines)

    def read_index(self) -> List[dict]:
        text = self.get_text("index.jsonl")
        if not text:
            return []
        return [json.loads(l) for l in text.splitlines() if l.strip()]


# ---- file:// / plain directory ----------------------------------------------------------------

class DirRemote(Remote):
    def __init__(self, url: str):
        self.url = url
        path = url[len("file://"):] if url.startswith("file://") else url
        self.root = Path(path).expanduser()

    def _p(self, key: str) -> Path:
        return self.root / key

    def put_file(self, local: Path, key: str) -> None:
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(local, tmp)
        os.replace(tmp, dst)

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._p(key), local)

    def get_text(self, key: str) -> Optional[str]:
        p = self._p(key)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def put_text(self, key: str, text: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def list_dirs(self, key_prefix: str) -> List[str]:
        p = self._p(key_prefix)
        return [d.name for d in p.iterdir() if d.is_dir()] if p.exists() else []

    def list_files(self, key_prefix: str) -> List[str]:
        p = self._p(key_prefix)
        if not p.exists():
            return []
        return [f"{key_prefix}{f.name}" for f in p.iterdir() if f.is_file() and not f.name.endswith(".tmp")]

    def delete_prefix(self, key_prefix: str) -> None:
        shutil.rmtree(self._p(key_prefix), ignore_errors=True)


# ---- s3:// ---------------------------------------------------------------------------------------

class S3Remote(Remote):
    """boto3 when available, otherwise the aws CLI. `client` can be injected (tests)."""

    def __init__(self, url: str, client=None, extra_args: Optional[dict] = None, profile: Optional[str] = None,
                 endpoint_url: Optional[str] = None):
        self.url = url.rstrip("/")
        u = urlparse(self.url)
        self.bucket = u.netloc
        self.prefix = u.path.lstrip("/")
        self.extra_args = extra_args or {}
        self.profile = profile
        self.endpoint_url = endpoint_url
        self._client = client
        if self._client is None:
            try:
                import boto3  # type: ignore
                session = boto3.Session(profile_name=profile) if profile else boto3.Session()
                self._client = session.client("s3", endpoint_url=endpoint_url)
            except ImportError:
                self._client = None
                if shutil.which("aws") is None:
                    raise RemoteError("s3:// remotes need either `pip install boto3` or the aws CLI on PATH")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _s3url(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    def _aws(self, *args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = ["aws"]
        if self.profile:
            cmd += ["--profile", self.profile]
        if self.endpoint_url:
            cmd += ["--endpoint-url", self.endpoint_url]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, input=input_text)
        return proc

    def put_file(self, local: Path, key: str) -> None:
        if self._client:
            self._client.upload_file(str(local), self.bucket, self._key(key), ExtraArgs=self.extra_args or None)
        else:
            args = ["s3", "cp", str(local), self._s3url(key), "--only-show-errors"]
            if self.extra_args.get("ServerSideEncryption"):
                args += ["--sse", self.extra_args["ServerSideEncryption"]]
            proc = self._aws(*args)
            if proc.returncode != 0:
                raise RemoteError(proc.stderr.strip())

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        if self._client:
            self._client.download_file(self.bucket, self._key(key), str(local))
        else:
            proc = self._aws("s3", "cp", self._s3url(key), str(local), "--only-show-errors")
            if proc.returncode != 0:
                raise RemoteError(proc.stderr.strip())

    def get_text(self, key: str) -> Optional[str]:
        if self._client:
            try:
                obj = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
            except Exception as e:  # noqa: BLE001 - botocore ClientError NoSuchKey / 404
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
                if code in ("NoSuchKey", "404", "NotFound") or "NoSuchKey" in str(e) or "Not Found" in str(e):
                    return None
                raise
            return obj["Body"].read().decode("utf-8")
        proc = self._aws("s3", "cp", self._s3url(key), "-")
        return proc.stdout if proc.returncode == 0 else None

    def put_text(self, key: str, text: str) -> None:
        if self._client:
            self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=text.encode("utf-8"),
                                    **self.extra_args)
        else:
            proc = self._aws("s3", "cp", "-", self._s3url(key), input_text=text)
            if proc.returncode != 0:
                raise RemoteError(proc.stderr.strip())

    def list_dirs(self, key_prefix: str) -> List[str]:
        full = self._key(key_prefix)
        if self._client:
            out = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full, Delimiter="/"):
                for cp in page.get("CommonPrefixes", []) or []:
                    out.append(cp["Prefix"][len(full):].rstrip("/"))
            return out
        proc = self._aws("s3", "ls", self._s3url(key_prefix))
        return [l.split("PRE", 1)[1].strip().rstrip("/") for l in proc.stdout.splitlines() if "PRE" in l]

    def list_files(self, key_prefix: str) -> List[str]:
        full = self._key(key_prefix)
        strip = len(self._key("")) if self.prefix else 0
        if self._client:
            out = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full, Delimiter="/"):
                for obj in page.get("Contents", []) or []:
                    out.append(obj["Key"][strip:])
            return out
        proc = self._aws("s3", "ls", self._s3url(key_prefix))
        names = [l.split()[-1] for l in proc.stdout.splitlines() if l.strip() and "PRE" not in l]
        return [f"{key_prefix}{n}" for n in names]

    def delete_prefix(self, key_prefix: str) -> None:
        if self._client:
            for key in self.list_files(key_prefix):
                self._client.delete_object(Bucket=self.bucket, Key=self._key(key))
        else:
            self._aws("s3", "rm", self._s3url(key_prefix), "--recursive", "--only-show-errors")


# ---- gs:// via gcloud CLI ---------------------------------------------------------------------

class GCSRemote(Remote):
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.tool = "gcloud" if shutil.which("gcloud") else ("gsutil" if shutil.which("gsutil") else None)
        if not self.tool:
            raise RemoteError("gs:// remotes need the gcloud (or gsutil) CLI on PATH")

    def _u(self, key: str) -> str:
        return f"{self.url}/{key}"

    def _run(self, *args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = ["gcloud", "storage", *args] if self.tool == "gcloud" else ["gsutil", *args]
        return subprocess.run(cmd, capture_output=True, text=True, input=input_text)

    def put_file(self, local: Path, key: str) -> None:
        proc = self._run("cp", str(local), self._u(key))
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.strip())

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run("cp", self._u(key), str(local))
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.strip())

    def get_text(self, key: str) -> Optional[str]:
        proc = self._run("cat", self._u(key))
        return proc.stdout if proc.returncode == 0 else None

    def put_text(self, key: str, text: str) -> None:
        proc = self._run("cp", "-", self._u(key), input_text=text)
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.strip())

    def list_dirs(self, key_prefix: str) -> List[str]:
        proc = self._run("ls", self._u(key_prefix))
        return [l.rstrip("/").rsplit("/", 1)[-1] for l in proc.stdout.splitlines() if l.endswith("/")]

    def list_files(self, key_prefix: str) -> List[str]:
        proc = self._run("ls", self._u(key_prefix))
        return [f"{key_prefix}{l.rsplit('/', 1)[-1]}" for l in proc.stdout.splitlines() if l and not l.endswith("/")]

    def delete_prefix(self, key_prefix: str) -> None:
        self._run("rm", "-r", self._u(key_prefix))


def open_remote(url: Optional[str] = None, cfg: Optional[dict] = None) -> Remote:
    cfg = cfg or config.load_config()
    rcfg = cfg.get("remote", {}) or {}
    url = url or os.environ.get("REPOGYM_REMOTE") or rcfg.get("url")
    if not url:
        raise RemoteError("no remote configured: `repogym remote set s3://bucket/prefix` or set REPOGYM_REMOTE")
    if url.startswith("s3://"):
        return S3Remote(url, extra_args=rcfg.get("s3_extra_args") or {}, profile=rcfg.get("aws_profile"),
                        endpoint_url=rcfg.get("s3_endpoint_url"))
    if url.startswith("gs://"):
        return GCSRemote(url)
    if url.startswith("file://") or "://" not in url:
        return DirRemote(url)
    raise RemoteError(f"unsupported remote scheme: {url}")


def split_task_url(url: str) -> Optional[tuple]:
    """'s3://b/p/tasks/<id>' -> ('s3://b/p', '<id>'); None if not a task URL."""
    marker = "/tasks/"
    if marker not in url:
        return None
    base, _, tid = url.rpartition(marker)
    tid = tid.strip("/")
    if not base or not tid or "/" in tid:
        return None
    return base, tid


# ---- sync bookkeeping ---------------------------------------------------------------------

def _marker_dir(store_root: Path) -> Path:
    d = store_root / "synced"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_synced(store_root: Path, task_id: str) -> bool:
    return (_marker_dir(store_root) / f"{task_id}.json").exists()


def mark_synced(store_root: Path, task_id: str, url: str) -> None:
    write_json(_marker_dir(store_root) / f"{task_id}.json", {"id": task_id, "remote": url, "synced_at": now_iso()})


def sync_task(store, task_id: str, remote: Optional[Remote] = None, purge_local: Optional[bool] = None,
              cfg: Optional[dict] = None) -> List[str]:
    """Push one local task to the remote, mark it, optionally delete the local copy."""
    cfg = cfg or config.load_config()
    rcfg = cfg.get("remote", {}) or {}
    remote = remote or open_remote(cfg=cfg)
    purge = rcfg.get("purge_local", False) if purge_local is None else purge_local
    task = read_json(store.task_dir(task_id) / "task.json") or {}
    if rcfg.get("mirror", True):
        _ensure_mirror_for_task(task, remote)
    pushed = remote.push_task(store.task_dir(task_id))
    mark_synced(store.root, task_id, remote.url)
    if purge:
        store.delete_task(task_id)
    return pushed


def _ensure_mirror_for_task(task: dict, remote: Remote) -> None:
    """The environment travels with the task: make sure its repository history is in the bucket."""
    repo_meta = task.get("repo") or {}
    repo_path = Path(repo_meta.get("path") or "")
    repo_id = repo_meta.get("id")
    if not repo_id or not repo_path.exists():
        return
    from .mirror import ensure_covered
    # Prefer covering the exact snapshot commits (they include head_commit as parent).
    for commit in (task.get("final_commit"), task.get("base_commit"), task.get("head_commit")):
        if commit and gitsnap_exists(repo_path, commit):
            ensure_covered(remote, repo_path, repo_id, repo_meta.get("name") or repo_path.name, commit,
                           url=repo_meta.get("remote"))
            return
    ensure_covered(remote, repo_path, repo_id, repo_meta.get("name") or repo_path.name, None,
                   url=repo_meta.get("remote"))


def gitsnap_exists(repo: Path, sha: str) -> bool:
    from . import gitsnap
    return gitsnap.commit_exists(repo, sha)


def sync_all(store, remote: Optional[Remote] = None, purge_local: Optional[bool] = None, force: bool = False,
             tiers: Optional[List[str]] = None, cfg: Optional[dict] = None, quiet: bool = False) -> int:
    cfg = cfg or config.load_config()
    remote = remote or open_remote(cfg=cfg)
    tiers = set(tiers or (cfg.get("remote", {}) or {}).get("tiers") or [])
    n = 0
    for task in store.tasks():
        if tiers and task.get("tier") not in tiers:
            continue
        if not force and is_synced(store.root, task["id"]):
            continue
        sync_task(store, task["id"], remote, purge_local, cfg)
        if not quiet:
            print(f"[repogym] pushed {task['id']} -> {remote.url}")
        n += 1
    return n


def cache_dir(store_root: Path) -> Path:
    d = store_root / "cache" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pull_task(store, task_id: str, remote: Optional[Remote] = None, refresh: bool = False) -> Path:
    """Fetch a task into the local cache (or return the cached/local copy)."""
    local = store.task_dir(task_id)
    if (local / "task.json").exists():
        return local
    dest = cache_dir(store.root) / task_id
    if (dest / "task.json").exists() and not refresh:
        return dest
    remote = remote or open_remote()
    return remote.pull_task(task_id, dest)


def iter_tasks(url: Optional[str] = None, tiers: Optional[List[str]] = None, use_index: bool = True) -> Iterator[dict]:
    """Iterate task metadata in a remote (index.jsonl when present, else list + fetch)."""
    remote = open_remote(url)
    metas = remote.read_index() if use_index else []
    if not metas:
        metas = [m for m in (remote.get_task_meta(t) for t in remote.list_task_ids()) if m]
    for m in metas:
        if tiers and m.get("tier") not in tiers:
            continue
        m = dict(m)
        m["remote_url"] = f"{remote.url}/tasks/{m['id']}"
        yield m
