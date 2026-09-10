"""Verify that every verified task in the store is solvable by its own reference patch."""
import sys

from repogym import Store
from repogym.verify import verify_patch


def main() -> int:
    store = Store()
    bad = 0
    for task in store.tasks():
        if task["tier"] not in ("verified", "suite"):
            continue
        tdir = store.task_dir(task["id"])
        gold = (tdir / "source.patch").read_text()
        info = verify_patch(task, tdir, gold, store_root=store.root)
        ok = info.get("success")
        print(f"{'OK ' if ok else 'BAD'} {task['id']}  reward={info.get('reward')}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
