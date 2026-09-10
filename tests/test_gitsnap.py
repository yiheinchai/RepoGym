import subprocess

from repogym import gitsnap


def test_snapshot_does_not_touch_working_tree_or_head(pyrepo):
    head_before = gitsnap.head_commit(pyrepo)
    (pyrepo / "calc" / "core.py").write_text("x = 1\n")
    (pyrepo / "newfile.txt").write_text("hello\n")
    (pyrepo / "ignored.pyc").write_text("binary")
    snap = gitsnap.snapshot(pyrepo, "t-base")
    assert snap.dirty is True
    assert snap.head == head_before
    assert gitsnap.head_commit(pyrepo) == head_before  # HEAD untouched
    status = subprocess.run(["git", "status", "--porcelain"], cwd=pyrepo, capture_output=True, text=True).stdout
    assert " M calc/core.py" in status and "?? newfile.txt" in status  # index untouched
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", snap.commit], cwd=pyrepo,
                           capture_output=True, text=True).stdout.split()
    assert "newfile.txt" in files and "ignored.pyc" not in files
    assert gitsnap.commit_exists(pyrepo, snap.commit)
    # pinned by a ref
    refs = subprocess.run(["git", "for-each-ref", "refs/repogym/"], cwd=pyrepo, capture_output=True, text=True).stdout
    assert "t-base" in refs


def test_clean_snapshot_is_not_dirty(pyrepo):
    snap = gitsnap.snapshot(pyrepo, "clean")
    assert snap.dirty is False


def test_diff_and_apply_roundtrip(pyrepo):
    a = gitsnap.snapshot(pyrepo, "a")
    (pyrepo / "calc" / "core.py").write_text("def add(a, b):\n    return a + b + 0\n")
    b = gitsnap.snapshot(pyrepo, "b")
    patch = gitsnap.diff(pyrepo, a.commit, b.commit)
    assert "+    return a + b + 0" in patch
    assert gitsnap.changed_files(pyrepo, a.commit, b.commit) == ["calc/core.py"]
    with gitsnap.worktree(pyrepo, a.commit) as wt:
        gitsnap.apply_patch(wt, patch)
        assert "+ 0" in (wt / "calc" / "core.py").read_text()
    assert not wt.exists()
