"""RepoGym through the gymnasium interface (pip install repogym[gym])."""
import json
import sys

from repogym.env import make_gymnasium_env


def main(task: str) -> None:
    env = make_gymnasium_env(task)
    obs, info = env.reset()
    print(json.loads(obs)["problem_statement"][:400])
    obs, reward, terminated, truncated, info = env.step(json.dumps({"type": "shell", "command": "git status --short"}))
    print(json.loads(obs)["last_output"])
    obs, reward, terminated, truncated, info = env.step(json.dumps({"type": "submit"}))
    print("reward", reward, info.get("success"))
    env.close()


if __name__ == "__main__":
    main(sys.argv[1])
