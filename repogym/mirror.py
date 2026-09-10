"""Repository mirrors in the bucket: the environment travels with the gym.

A task is a delta on top of a repository's history. Without that history in the bucket, tasks
depend on the git host still having the commit, under the same URL, forever. So RepoGym mirrors
each repository into the bucket once, then appends only what is new:

    <prefix>/repos/<repo_id>/manifest.json
    <prefix>/repos/<repo_id>/bundles/0000-<sha>.bundle    full bundle: all refs (incl. snapshot pins)
    <prefix>/repos/<repo_id>/bundles/0001-<sha>.bundle    thin bundle: objects not in the previous heads
    ...

Git bundles are packfiles, so dedup is git's own content addressing. Restoring is fetching the
bundles in order into a bare-ish clone. A rename, deletion or history rewrite on the git host
changes nothing about what the bucket can reproduce.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from . import gitsnap
from .remote import Remote, RemoteError
from .util import git, now_iso, read_json, write_json

MIRROR_STATE = ".repogym-mirror.json"   # inside a restored clone's .git: which bundles were applied


def repo_key(repo_id: str, name: str = "") -> str:
    return f"repos/{repo_id}/{name}" if name else f"repos/{repo_id}/"


def get_manifest(remote: Remote, repo_id: str) -> Optional[dict]:
    text = remote.get_text(repo_key(repo_id, "manifest.json"))
    return json.loads(text) if text else None


def _all_heads(repo: Path) -> Dict[str, str]:
    out = git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/"], cwd=repo).stdout
    heads = {}
    for line in out.splitlines():
        ref, _, sha = line.partition(" ")
        if ref and sha:
            heads[ref] = sha
    return heads


def covers(manifest: Optional[dict], repo: Path, commit: str) -> bool:
    """Is `commit` already reachable from heads recorded in the mirror? (Checked locally.)"""
    if not manifest or not commit:
        return False
    covered = [s for s in manifest.get("covered", []) if gitsnap.commit_exists(repo, s)]
    for sha in covered:
        if sha == commit:
            return True
        if git(["merge-base", "--is-ancestor", commit, sha], cwd=repo, check=False).returncode == 0:
            return True
    return False


def push_mirror(remote: Remote, repo: Path, repo_id: str, name: str, url: Optional[str] = None,
                force_full: bool = False) -> Optional[dict]:
    """Upload a full bundle on first contact, then thin increments. Returns the bundle record or None."""
    repo = Path(repo)
    manifest = None if force_full else get_manifest(remote, repo_id)
    heads = _all_heads(repo)
    if not heads:
        return None
    prev_covered = [s for s in (manifest or {}).get("covered", []) if gitsnap.commit_exists(repo, s)]
    if manifest:
        new = git(["rev-list", "--all", "--not", *prev_covered], cwd=repo, check=False).stdout.strip() \
            if prev_covered else "x"
        if not new:
            return None  # nothing new since the last bundle
    seq = len((manifest or {}).get("bundles", []))
    tip = git(["rev-parse", "--short=12", "HEAD"], cwd=repo, check=False).stdout.strip() or "nohead"
    fname = f"{seq:04d}-{tip}.bundle"
    fd, tmp = tempfile.mkstemp(prefix="repogym-mirror-", suffix=".bundle")
    os.close(fd)
    try:
        args = ["bundle", "create", "--quiet", tmp, "--all"] + [f"^{s}" for s in prev_covered]
        proc = git(args, cwd=repo, check=False)
        if proc.returncode != 0:
            # Prerequisites may be unusable (e.g. gc'd); fall back to a full bundle.
            git(["bundle", "create", "--quiet", tmp, "--all"], cwd=repo)
            prev_covered = []
        size = os.path.getsize(tmp)
        remote.put_file(Path(tmp), repo_key(repo_id, f"bundles/{fname}"))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    record = {"name": fname, "created_at": now_iso(), "bytes": size, "thin": bool(prev_covered),
              "heads": heads}
    covered = sorted(set(prev_covered) | set(heads.values()))
    manifest = {
        "repo_id": repo_id, "name": name, "remote": url or (manifest or {}).get("remote"),
        "updated_at": now_iso(),
        "bundles": ((manifest or {}).get("bundles", []) + [record]),
        "covered": covered,
    }
    remote.put_text(repo_key(repo_id, "manifest.json"), json.dumps(manifest, indent=1))
    return record


def ensure_covered(remote: Remote, repo: Path, repo_id: str, name: str, commit: Optional[str],
                   url: Optional[str] = None) -> bool:
    """Make sure `commit` (and everything reachable from it) is in the bucket. Returns True if pushed."""
    manifest = get_manifest(remote, repo_id)
    if commit and covers(manifest, repo, commit):
        return False
    return push_mirror(remote, repo, repo_id, name, url) is not None


def restore_mirror(remote: Remote, repo_id: str, dest: Path) -> Optional[Path]:
    """Create/refresh a local clone from the bucket mirror. Returns dest, or None if no mirror exists."""
    manifest = get_manifest(remote, repo_id)
    if not manifest or not manifest.get("bundles"):
        return None
    dest = Path(dest)
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        git(["init", "--quiet"], cwd=dest)
    state_path = dest / ".git" / MIRROR_STATE
    state = read_json(state_path, {"applied": []}) or {"applied": []}
    applied = set(state.get("applied", []))
    for rec in manifest["bundles"]:
        if rec["name"] in applied:
            continue
        fd, tmp = tempfile.mkstemp(prefix="repogym-restore-", suffix=".bundle")
        os.close(fd)
        try:
            remote.get_file(repo_key(repo_id, f"bundles/{rec['name']}"), Path(tmp))
            # Bring in every ref the bundle carries; snapshot pins land under refs/repogym/.
            proc = git(["fetch", "--quiet", "--force", tmp, "+refs/heads/*:refs/remotes/mirror/*",
                        "+refs/tags/*:refs/tags/*", "+refs/repogym/*:refs/repogym/*"], cwd=dest, check=False)
            if proc.returncode != 0:
                raise RemoteError(f"could not apply mirror bundle {rec['name']}: {proc.stderr.strip()}")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        applied.add(rec["name"])
        write_json(state_path, {"applied": sorted(applied), "repo_id": repo_id})
    if manifest.get("remote"):
        git(["remote", "remove", "origin"], cwd=dest, check=False)
        git(["remote", "add", "origin", manifest["remote"]], cwd=dest, check=False)
    return dest


def mirror_status(remote: Remote, repo_id: str) -> Optional[dict]:
    m = get_manifest(remote, repo_id)
    if not m:
        return None
    return {"repo_id": repo_id, "name": m.get("name"), "bundles": len(m["bundles"]),
            "bytes": sum(b.get("bytes", 0) for b in m["bundles"]), "covered": len(m.get("covered", [])),
            "updated_at": m.get("updated_at")}
