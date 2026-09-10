"""Training-side loader: iterate the organisation's gym straight from the bucket.

    REPOGYM_REMOTE=s3://acme-repogym/gym python examples/train_loader.py --tier verified --limit 5

Each iteration yields a ready RepoGymEnv; plug your policy in where `random_policy` is.
"""
import argparse
import random

from repogym import RepoGymEnv
from repogym.remote import iter_tasks


def random_policy(env, obs):
    """Placeholder policy: look around, then submit the reference patch half the time."""
    env.step("git status --short && ls")
    if random.random() < 0.5:
        env.step({"type": "patch", "diff": env.gold_source_patch})
    return env.step({"type": "submit"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote", default=None, help="defaults to REPOGYM_REMOTE / config")
    ap.add_argument("--tier", action="append", default=None)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    total = 0.0
    n = 0
    for meta in iter_tasks(args.remote, tiers=args.tier or ["verified", "suite"]):
        if n >= args.limit:
            break
        env = RepoGymEnv(meta["remote_url"])
        obs, info = env.reset()
        _, reward, _, _, result = random_policy(env, obs)
        env.close()
        print(f"{meta['id']:<70} reward={reward:.2f} success={result.get('success')}")
        total += reward
        n += 1
    if n:
        print(f"\nmean reward over {n} task(s): {total / n:.3f}")


if __name__ == "__main__":
    main()
