"""Git snapshots of a working tree, without touching the working tree.

A snapshot is a real commit object created from a *temporary index*: we stage everything
(`git add -A`, so .gitignore is respected) into a throwaway index, `write-tree` it and
`commit-tree` it with the current HEAD as parent. The user's index, HEAD and working tree
are never modified. The commit is pinned by a ref under refs/repogym/ so gc keeps it.

This is the same trick `git stash create` uses, and it is what makes RepoGym cheap enough to
run from an editor hook: a snapshot of a large repo takes well under a second when the
index is warm.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from .util import CommandError, git

REF_PREFIX = "refs/repogym/snapshots"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass
class Snapshot:
    commit: str            # synthetic commit capturing the full working tree
    tree: str
    head: Optional[str]    # real HEAD at snapshot time (None on an unborn branch)
    branch: Optional[str]
    dirty: bool            # working tree differed from HEAD
    ref: str
    skipped_large: List[str] = field(default_factory=list)  # untracked files left out for size

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Snapshot":
        return Snapshot(**{k: d.get(k) for k in ("commit", "tree", "head", "branch", "dirty", "ref")},
                        skipped_large=list(d.get("skipped_large") or []))


def find_repo_root(path: os.PathLike) -> Optional[Path]:
    p = Path(path).resolve()
    if not p.exists():
        return None
    if p.is_file():
        p = p.parent
    proc = git(["rev-parse", "--show-toplevel"], cwd=p, check=False)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def git_dir(repo: Path) -> Path:
    return Path(git(["rev-parse", "--absolute-git-dir"], cwd=repo).stdout.strip())


def head_commit(repo: Path) -> Optional[str]:
    proc = git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo, check=False)
    return proc.stdout.strip() or None


def current_branch(repo: Path) -> Optional[str]:
    proc = git(["symbolic-ref", "-q", "--short", "HEAD"], cwd=repo, check=False)
    return proc.stdout.strip() or None


def remote_url(repo: Path) -> Optional[str]:
    proc = git(["config", "--get", "remote.origin.url"], cwd=repo, check=False)
    url = proc.stdout.strip() or None
    if url:
        # Strip embedded credentials from https://user:token@host/... style remotes.
        import re
        url = re.sub(r"://[^/@]+@", "://", url)
    return url


def large_untracked_files(repo: Path, max_bytes: int) -> List[str]:
    """Untracked, non-ignored files bigger than max_bytes. These are almost never source code."""
    if max_bytes <= 0:
        return []
    out = git(["ls-files", "-o", "--exclude-standard", "-z"], cwd=repo).stdout
    big = []
    for rel in out.split("\0"):
        if not rel:
            continue
        try:
            if os.path.getsize(repo / rel) > max_bytes:
                big.append(rel)
        except OSError:
            continue
    return big


def snapshot(repo: Path, label: str, max_file_size_mb: float = 0) -> Snapshot:
    """Create a pinned snapshot commit of the current working tree of `repo`.

    Untracked files larger than max_file_size_mb (0 = no limit) are excluded so a stray build
    artifact cannot bloat the repository's object store.
    """
    repo = Path(repo)
    gdir = git_dir(repo)
    head = head_commit(repo)
    skipped = large_untracked_files(repo, int(max_file_size_mb * 1024 * 1024)) if max_file_size_mb else []
    fd, tmp_index = tempfile.mkstemp(prefix="repogym-index-", dir=str(gdir))
    os.close(fd)
    os.unlink(tmp_index)  # git wants to create it itself
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        if head:
            git(["read-tree", head], cwd=repo, env=env)
        # Stage everything (respects .gitignore). -f is *not* used: ignored files stay out.
        add_args = ["add", "-A", "--", "."] + [f":(exclude,literal){p}" for p in skipped]
        git(add_args, cwd=repo, env=env)
        tree = git(["write-tree"], cwd=repo, env=env).stdout.strip()
        head_tree = git(["rev-parse", f"{head}^{{tree}}"], cwd=repo).stdout.strip() if head else EMPTY_TREE
        dirty = tree != head_tree
        args = ["commit-tree", tree, "-m", f"repogym snapshot: {label}"]
        if head:
            args += ["-p", head]
        commit_env = {
            "GIT_AUTHOR_NAME": "repogym", "GIT_AUTHOR_EMAIL": "repogym@localhost",
            "GIT_COMMITTER_NAME": "repogym", "GIT_COMMITTER_EMAIL": "repogym@localhost",
        }
        commit = git(args, cwd=repo, env=commit_env).stdout.strip()
        ref = f"{REF_PREFIX}/{label}"
        git(["update-ref", ref, commit], cwd=repo)
        return Snapshot(commit=commit, tree=tree, head=head, branch=current_branch(repo), dirty=dirty, ref=ref,
                        skipped_large=skipped)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_index)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_index + ".lock")


def delete_snapshot_ref(repo: Path, ref: str) -> None:
    git(["update-ref", "-d", ref], cwd=repo, check=False)


def snapshot_refs(repo: Path) -> List[str]:
    out = git(["for-each-ref", "--format=%(refname)", REF_PREFIX], cwd=repo, check=False).stdout
    return [l for l in out.splitlines() if l.strip()]


def snapshot_footprint(repo: Path) -> Tuple[int, int]:
    """(object count, bytes) reachable only from snapshot refs, i.e. what RepoGym adds to .git."""
    refs = snapshot_refs(repo)
    if not refs:
        return 0, 0
    objs = git(["rev-list", "--objects", *refs, "--not", "--branches", "--remotes", "--tags"], cwd=repo,
               check=False).stdout
    ids = [l.split()[0] for l in objs.splitlines() if l.strip()]
    if not ids:
        return 0, 0
    from .util import run
    proc = run(["git", "cat-file", "--batch-check=%(objectsize)"], cwd=repo, check=False,
               input_text="\n".join(ids) + "\n")
    total = 0
    for line in proc.stdout.splitlines():
        try:
            total += int(line.strip())
        except ValueError:
            pass
    return len(ids), total


def garbage_collect(repo: Path, prune_now: bool = False) -> None:
    """Run git gc. Unpinned snapshot objects are pruned after git's default expiry (2 weeks)
    unless prune_now is set."""
    args = ["gc", "--quiet"]
    if prune_now:
        args.append("--prune=now")
    git(args, cwd=repo, check=False, timeout=1800)


def diff(repo: Path, a: str, b: str, paths: Sequence[str] = (), exclude: Sequence[str] = (),
         binary: bool = True) -> str:
    """Unified diff between two commits, optionally restricted to / excluding pathspecs."""
    args = ["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"]
    if binary:
        args.append("--binary")
    args += [a, b, "--"]
    if paths:
        args += list(paths)
    else:
        args.append(".")
    for ex in exclude:
        args.append(f":(exclude,glob){ex}")
    return git(args, cwd=repo).stdout


def changed_files(repo: Path, a: str, b: str) -> List[str]:
    out = git(["diff", "--name-only", "--no-renames", a, b], cwd=repo).stdout
    return [line for line in out.splitlines() if line.strip()]


def diff_stat_lines(patch: str) -> int:
    n = 0
    for line in patch.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
            n += 1
    return n


def commit_exists(repo: Path, sha: str) -> bool:
    proc = git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, check=False)
    return proc.returncode == 0


def apply_patch(repo: Path, patch: str, reverse: bool = False) -> None:
    """Apply a unified diff to the working tree of `repo` (index untouched)."""
    if not patch.strip():
        return
    args = ["apply", "--binary", "--whitespace=nowarn"]
    if reverse:
        args.append("-R")
    # git() does not accept stdin; use run directly.
    from .util import run
    env = dict(os.environ, GIT_PAGER="cat")
    proc = run(["git", *args], cwd=repo, env=env, check=False, input_text=patch)
    if proc.returncode != 0:
        # Retry with 3-way merge (helps when context drifted slightly).
        proc2 = run(["git", *args, "--3way"], cwd=repo, env=env, check=False, input_text=patch)
        if proc2.returncode != 0:
            raise CommandError(["git", *args], proc.returncode, proc.stdout, proc.stderr or proc2.stderr)


@contextlib.contextmanager
def worktree(repo: Path, commit: str, base_dir: Optional[Path] = None, keep: bool = False) -> Iterator[Path]:
    """Check out `commit` into a scratch git worktree; removed on exit unless keep."""
    base_dir = Path(base_dir) if base_dir else Path(tempfile.gettempdir())
    base_dir.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="repogym-wt-", dir=str(base_dir)))
    # mkdtemp created the dir; git worktree add wants a non-existent or empty dir. Empty is fine.
    git(["worktree", "add", "--detach", "--force", str(path), commit], cwd=repo)
    try:
        yield path
    finally:
        if not keep:
            git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
            shutil.rmtree(path, ignore_errors=True)
            git(["worktree", "prune"], cwd=repo, check=False)


def link_ignored_dirs(src_repo: Path, dst: Path, names: Sequence[str]) -> List[str]:
    """Symlink heavy, git-ignored dependency dirs (node_modules, .venv...) into a scratch checkout."""
    linked = []
    for name in names:
        s = src_repo / name
        d = dst / name
        if s.exists() and not d.exists():
            try:
                os.symlink(s, d, target_is_directory=s.is_dir())
                linked.append(name)
            except OSError:
                pass
    return linked


def export_bundle(repo: Path, out: Path, refs: Sequence[str]) -> None:
    """Write a git bundle containing the given refs so snapshots can travel to other machines."""
    git(["bundle", "create", str(out), *refs], cwd=repo)
