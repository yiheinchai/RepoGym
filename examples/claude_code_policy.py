"""Use Claude Code itself as the policy: `claude -p` works inside the env's scratch checkout,
then RepoGym grades the result with the hidden tests.

    python examples/claude_code_policy.py <task-id> [--model claude-sonnet-5]
"""
import argparse
import subprocess
import sys

from repogym import RepoGymEnv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    env = RepoGymEnv(args.task)
    obs, info = env.reset()
    prompt = (
        "You are working in a git checkout. Solve the following task by editing the code. "
        "Run the existing tests to check your work. Do not commit.\n\n" + obs["problem_statement"]
    )
    cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    if args.model:
        cmd += ["--model", args.model]
    print(f"[policy] running Claude Code in {obs['repo_path']}", flush=True)
    subprocess.run(cmd, cwd=obs["repo_path"], timeout=args.timeout)
    obs, reward, terminated, truncated, info = env.step({"type": "submit"})
    print(obs["last_output"])
    env.close()
    return 0 if info.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
