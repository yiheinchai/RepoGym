"""Gym-style environment over a RepoGym task.

    from repogym import RepoGymEnv
    env = RepoGymEnv("~/.repogym/tasks/myrepo__fix-retry__1a2b3c4d")
    obs, info = env.reset()
    obs, reward, terminated, truncated, info = env.step("pytest tests/test_retry.py -q")
    obs, reward, terminated, truncated, info = env.step({"type": "patch", "diff": my_patch})
    obs, reward, terminated, truncated, info = env.step({"type": "submit"})
    env.close()

The agent works in a scratch git worktree checked out at the task's base commit. Hidden tests
(the engineer's test patch) are applied only at submit time, so the policy is graded on the
same evidence the original engineer used, without being able to read the answer.

Actions (dict, or a plain string which means shell):
    {"type": "shell",  "command": "..."}          run a command in the repo, observe output
    {"type": "read",   "path": "..."}             read a file
    {"type": "write",  "path": "...", "content"}  write a file (creates directories)
    {"type": "patch",  "diff": "..."}             apply a unified diff
    {"type": "submit"}                            grade the current working tree and end the episode
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import config, gitsnap
from .store import Store
from .util import git, read_json, truncate
from .verify import (TIER_SUITE, TIER_VERIFIED, locate_repo, materialize, reset_paths, run_task_tests, score,
                     task_files)

Obs = Dict[str, Any]


def resolve_task(task: str, store: Store) -> Tuple[dict, Path, Any]:
    """Accept a task directory, a local task id, a remote task URL (s3://.../tasks/<id>) or an id
    that only exists in the configured remote. Returns (task.json, local task dir, remote or None)."""
    from .remote import pull_task, split_task_url, open_remote
    p = Path(str(task)).expanduser()
    if (p / "task.json").exists():
        return read_json(p / "task.json"), p, None
    local = store.task_dir(str(task))
    if (local / "task.json").exists():
        return read_json(local / "task.json"), local, None
    parts = split_task_url(str(task))
    if parts:
        base_url, tid = parts
        remote = open_remote(base_url)
        tdir = pull_task(store, tid, remote=remote)
        return read_json(tdir / "task.json"), tdir, remote
    cached = store.root / "cache" / "tasks" / str(task)
    if (cached / "task.json").exists():
        return read_json(cached / "task.json"), cached, None
    remote = open_remote()
    tdir = pull_task(store, str(task), remote=remote)  # configured remote
    return read_json(tdir / "task.json"), tdir, remote


class RepoGymEnv:
    metadata = {"render_modes": ["ansi"]}

    def __init__(self, task: str, store: Optional[Store] = None, cfg: Optional[dict] = None,
                 repo: Optional[os.PathLike] = None, workdir: Optional[os.PathLike] = None,
                 hidden_tests: bool = True, timeout: Optional[float] = None, shell_timeout: float = 300,
                 max_steps: Optional[int] = None, partial_credit: bool = True, keep: bool = False,
                 output_limit: int = 20000):
        self.store = store or Store()
        self.cfg = cfg or config.load_config()
        self.task, self.task_dir, bucket = resolve_task(task, self.store)
        self.files = task_files(self.task_dir)
        self.repo = Path(repo) if repo else locate_repo(self.task, self.store.root)
        if self.repo is None:
            # Training box: no engineer checkout here. Restore from the bucket mirror, else clone.
            from .clones import ensure_repo
            self.repo = ensure_repo(self.task, self.task_dir, self.store.root, bucket=bucket)
        self.workdir = Path(workdir) if workdir else None
        self.hidden_tests = hidden_tests
        self.timeout = timeout
        self.shell_timeout = shell_timeout
        self.max_steps = max_steps
        self.partial_credit = partial_credit
        self.keep = keep
        self.output_limit = output_limit
        self._ctx = None
        self.repo_path: Optional[Path] = None
        self.steps = 0
        self.done = False

    # ---- gym API -----------------------------------------------------------------------------
    def reset(self, seed: Optional[int] = None, options: Optional[dict] = None) -> Tuple[Obs, dict]:
        self.close()
        self._ctx = materialize(self.task, self.repo, self.cfg, base_dir=self.workdir, keep=self.keep,
                                task_dir=self.task_dir, store_root=self.store.root)
        self.repo_path = self._ctx.__enter__()
        from .verify import LAST_DEPS_STATUS
        self.deps_status = dict(LAST_DEPS_STATUS)
        self.steps = 0
        self.done = False
        return self._obs(last_output=""), {"task_id": self.task["id"], "tier": self.task.get("tier"),
                                           "deps": self.deps_status}

    def step(self, action) -> Tuple[Obs, float, bool, bool, dict]:
        if self.done or self.repo_path is None:
            raise RuntimeError("call reset() before step()")
        if isinstance(action, str):
            action = {"type": "shell", "command": action}
        kind = (action.get("type") or "shell").lower()
        self.steps += 1
        reward = 0.0
        info: dict = {}
        output = ""
        if kind == "shell":
            output = self._shell(action.get("command", ""))
        elif kind == "read":
            output = self._read(action.get("path", ""))
        elif kind == "write":
            output = self._write(action.get("path", ""), action.get("content", ""))
        elif kind == "patch":
            output = self._patch(action.get("diff", ""))
        elif kind == "submit":
            info = self.evaluate()
            reward = float(info.get("reward") or 0.0)
            output = _format_eval(info)
            self.done = True
        else:
            output = f"unknown action type: {kind}"
        truncated = bool(self.max_steps and self.steps >= self.max_steps and not self.done)
        if truncated:
            self.done = True
        return self._obs(last_output=output), reward, self.done and not truncated, truncated, info

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.__exit__(None, None, None)
            finally:
                self._ctx = None
                self.repo_path = None

    def render(self) -> str:
        return f"[{self.task['id']}] step={self.steps} repo={self.repo_path}"

    # ---- helpers -----------------------------------------------------------------------------
    @property
    def problem_statement(self) -> str:
        return self.files["problem"]

    @property
    def gold_patch(self) -> str:
        """The engineer's own solution (source + tests). For imitation / debugging, not for the policy."""
        return self.files["solution"]

    @property
    def gold_source_patch(self) -> str:
        p = self.task_dir / "source.patch"
        return p.read_text(encoding="utf-8") if p.exists() else self.files["solution"]

    def current_patch(self) -> str:
        """Diff of the agent's working tree against the base commit (includes new files)."""
        assert self.repo_path is not None
        git(["add", "-A", "--", "."], cwd=self.repo_path, check=False)
        out = git(["diff", "--cached", "--binary", "--no-color", "HEAD"], cwd=self.repo_path).stdout
        git(["reset", "-q"], cwd=self.repo_path, check=False)
        return out

    def evaluate(self) -> dict:
        """Grade the current working tree in place: apply hidden tests, run, score."""
        assert self.repo_path is not None
        candidate = self.current_patch()
        result = None
        if self.task.get("runner") and self.task.get("tier") in (TIER_VERIFIED, TIER_SUITE):
            if self.hidden_tests and self.files["test"]:
                reset_paths(self.repo_path, "HEAD", self.task.get("test_files") or [])
                gitsnap.apply_patch(self.repo_path, self.files["test"])
            result = run_task_tests(self.task, self.repo_path, self.cfg, self.timeout)
        info = score(self.task, result, candidate, self.gold_patch, self.partial_credit)
        info["candidate_patch_lines"] = gitsnap.diff_stat_lines(candidate)
        if result is not None:
            info["test_summary"] = result.summary()
            info["stdout_tail"] = result.stdout[-4000:]
            info["stderr_tail"] = result.stderr[-2000:]
        return info

    def _obs(self, last_output: str) -> Obs:
        return {
            "task_id": self.task["id"],
            "title": self.task.get("title"),
            "problem_statement": self.problem_statement,
            "language": self.task.get("language"),
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "runner": self.task.get("runner"),
            "deps_ready": not (getattr(self, "deps_status", {}) or {}).get("failed"),
            "step": self.steps,
            "last_output": truncate(last_output, self.output_limit),
        }

    def shell_env(self) -> dict:
        """Environment for agent shell commands: provisioned venv / node_modules/.bin first on PATH."""
        env = {**os.environ, "REPOGYM_TASK": self.task["id"], "CI": "1"}
        path_parts = []
        venv = self.repo_path / ".venv"
        if (venv / "bin").is_dir():
            path_parts.append(str(venv / "bin"))
            env["VIRTUAL_ENV"] = str(venv.resolve())
        nbin = self.repo_path / "node_modules" / ".bin"
        if nbin.is_dir():
            path_parts.append(str(nbin))
        if path_parts:
            env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])
        return env

    def _shell(self, command: str) -> str:
        if not command.strip():
            return ""
        try:
            proc = subprocess.run(command, shell=True, cwd=str(self.repo_path), capture_output=True, text=True,
                                  errors="replace", timeout=self.shell_timeout, stdin=subprocess.DEVNULL,
                                  env=self.shell_env())
            out = proc.stdout
            if proc.stderr:
                out += ("\n" if out else "") + proc.stderr
            return out + f"\n[exit {proc.returncode}]"
        except subprocess.TimeoutExpired:
            return f"[timeout after {self.shell_timeout}s]"

    def _safe_path(self, rel: str) -> Path:
        p = (self.repo_path / rel).resolve()
        if self.repo_path.resolve() not in p.parents and p != self.repo_path.resolve():
            raise ValueError("path escapes the repository")
        return p

    def _read(self, rel: str) -> str:
        try:
            return self._safe_path(rel).read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return f"[error: {e}]"

    def _write(self, rel: str, content: str) -> str:
        try:
            p = self._safe_path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"[wrote {rel} ({len(content)} chars)]"
        except Exception as e:  # noqa: BLE001
            return f"[error: {e}]"

    def _patch(self, diff: str) -> str:
        try:
            gitsnap.apply_patch(self.repo_path, diff)
            return "[patch applied]"
        except Exception as e:  # noqa: BLE001
            return f"[patch failed: {truncate(str(e), 2000)}]"


def _format_eval(info: dict) -> str:
    lines = [f"tier={info.get('tier')} success={info.get('success')} reward={info.get('reward')}"]
    if "fail_to_pass_total" in info:
        lines.append(f"FAIL_TO_PASS {info['fail_to_pass_passed']}/{info['fail_to_pass_total']} "
                     f"PASS_TO_PASS failures={len(info.get('pass_to_pass_failed', []))}")
    if info.get("similarity") is not None:
        lines.append(f"similarity_to_reference={info['similarity']:.3f}")
    return "\n".join(lines)


def make_gymnasium_env(task: str, **kwargs):
    """Wrap RepoGymEnv in a gymnasium.Env (requires the `gym` extra)."""
    import gymnasium as gym  # type: ignore
    from gymnasium import spaces  # type: ignore
    import json as _json

    class _GymnasiumRepoGym(gym.Env):
        metadata = {"render_modes": ["ansi"]}

        def __init__(self):
            super().__init__()
            self.inner = RepoGymEnv(task, **kwargs)
            self.observation_space = spaces.Text(max_length=1_000_000)
            self.action_space = spaces.Text(max_length=1_000_000)

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            obs, info = self.inner.reset(seed=seed, options=options)
            return _json.dumps(obs), info

        def step(self, action):
            try:
                act = _json.loads(action)
            except Exception:
                act = {"type": "shell", "command": action}
            obs, r, term, trunc, info = self.inner.step(act)
            return _json.dumps(obs), r, term, trunc, info

        def render(self):
            return self.inner.render()

        def close(self):
            self.inner.close()

    return _GymnasiumRepoGym()
