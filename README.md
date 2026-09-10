# RepoGym

**Turn your engineers' everyday Claude Code and Codex sessions into a private RL gym.**

Every time an engineer asks a coding agent to fix a bug or ship a feature, three things are
produced: a task description (the prompt), a starting state (the repo before), and a verified
solution (the repo after, plus the tests that now pass). That is exactly an RL environment.
Frontier labs already harvest this from *your* usage. RepoGym captures it for **you**, on
your machines, so your organisation owns a growing arsenal of verifiable coding tasks built
from its own codebases and can train its own models on them later.

Power to the people.

```
  engineer + Claude Code / Codex           RepoGym (async)                     your training stack
 ┌──────────────────────────────┐   ┌───────────────────────────────┐   ┌─────────────────────────┐
 │ prompt ─► agent edits ─► stop │──►│ snapshot base / final         │──►│ RepoGymEnv.reset()      │
 │  (hooks fire, ~100 ms each)  │   │ split source / test patches   │   │ .step(shell|patch|...)  │
 └──────────────────────────────┘   │ run tests before & after      │   │ .step(submit) -> reward │
                                    │ derive FAIL_TO_PASS/PASS_TO_PASS│  │ export --format swebench│
                                    └───────────────────────────────┘   └─────────────────────────┘
```

## What you get

- **Zero-friction capture.** `repogym install claude-code` adds hooks to Claude Code; `repogym install codex`
  registers a Codex `notify` handler. Hooks return in ~100 ms and never block or alter the agent.
  Anything else works through `repogym wrap -- <agent command>`.
- **Verified tasks, not just diffs.** The builder checks out the pre-task state in a scratch worktree,
  applies the engineer's test changes, runs the suite, applies the source changes, runs it again, and
  records which tests flip from failing to passing (`FAIL_TO_PASS`) and which keep passing
  (`PASS_TO_PASS`). Same semantics as SWE-bench, so rewards are executable and cheat-resistant.
- **A gym.** `RepoGymEnv` gives a policy a fresh checkout at the base commit, lets it run shell commands,
  read/write files or apply patches, and grades a `submit` with the hidden tests. A gymnasium wrapper
  is included.
- **Interop.** `repogym export --format swebench` writes JSONL that drops into SWE-agent, OpenHands,
  mini-swe-agent, `verifiers` or your own harness. Snapshots travel as git bundles.
- **Trajectories too.** Each task stores the scrubbed tool-call trajectory of the original session
  (which files were read, what commands were run) for SFT / warm-starting.
- **Privacy by construction.** Everything stays local under `~/.repogym`. Credentials are redacted from
  prompts, commands and patches; secret-looking files are excluded; any engineer or repo can opt out.
  LLM-based problem-statement rewriting is optional and off by default.

Supported test runners for per-test verification: pytest, jest, vitest, `go test`, `cargo test`,
rspec, phpunit. Others (make test, npm test, gradle, maven, dotnet, mix) fall back to a suite-level
exit-code verifier.

## Quick start

```bash
pip install -e .            # or: pipx install .     (no required dependencies)
repogym init
repogym install claude-code # writes hooks to ~/.claude/settings.json  (--scope project for one repo)
repogym install codex       # writes notify = ["repogym", "codex-notify"] to ~/.codex/config.toml
```

Now just work. Each prompt → stop cycle that changes files becomes an episode; a detached builder
turns it into a task within seconds to minutes (it runs your tests twice).

```bash
repogym status
repogym list                # tasks with tier / F2P / P2P counts
repogym show <task-id>      # problem statement + which tests must flip
repogym verify <task-id> --gold        # sanity: the engineer's own patch scores 1.0
repogym export --format swebench -o gym.jsonl --tier verified
```

### Using the gym

```python
from repogym import RepoGymEnv

env = RepoGymEnv("myrepo__fix-divide-by-zero__1a2b3c4d")     # task id or task directory
obs, info = env.reset()
print(obs["problem_statement"], obs["repo_path"])

obs, r, term, trunc, info = env.step("pytest -q")                       # str == shell command
obs, r, term, trunc, info = env.step({"type": "read", "path": "calc/core.py"})
obs, r, term, trunc, info = env.step({"type": "patch", "diff": my_unified_diff})
obs, reward, term, trunc, info = env.step({"type": "submit"})           # hidden tests run here
env.close()
```

`reward` is 1.0 when every `FAIL_TO_PASS` test passes and no `PASS_TO_PASS` test regresses
(partial credit for a fraction of F2P with no regressions; configurable). See
[`examples/`](examples/) for a policy that uses Claude Code itself as the agent, and for the
gymnasium wrapper.

## How it works

1. **Snapshots without side effects.** RepoGym never touches the engineer's index, HEAD or working
   tree. It stages the tree into a *temporary index*, `write-tree`s it and `commit-tree`s it with the
   current HEAD as parent, then pins the commit under `refs/repogym/snapshots/…`. That is the same
   trick `git stash create` uses; it respects `.gitignore` and takes well under a second on warm repos.
2. **Episodes.** `UserPromptSubmit` opens an episode (base snapshot). `PostToolUse` appends a scrubbed
   tool record; Bash commands that look like test invocations tell the builder how *you* run tests.
   `Stop` closes it (final snapshot) and enqueues a build. Follow-up prompts in the same session chain:
   the next base is the previous final. `--merge-session` also builds one coarse task per session.
3. **Build.** Diff base→final, drop excluded files, split into `test.patch` and `source.patch`, pick a
   runner (what the engineer ran wins, else project files), then run before/after in a scratch worktree
   with `node_modules` / `.venv` etc. symlinked in. Tier the result:

   | tier | meaning | reward |
   |---|---|---|
   | `verified` | per-test F2P/P2P found | executable, SWE-bench semantics |
   | `suite` | only the whole suite flips fail→pass | exit code |
   | `unverified` | no discriminating tests | none; similarity to the reference patch is reported as advisory |

4. **Task layout** (`~/.repogym/tasks/<id>/`): `task.json`, `problem.md`, `solution.patch`,
   `source.patch`, `test.patch`, `base.patch` (uncommitted state the engineer started from, relative to
   `head_commit`, so tasks are reproducible from the real commit history), `trajectory.jsonl`.

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Configuration

`~/.repogym/config.json` (or `repogym config set key value`):

| key | default | purpose |
|---|---|---|
| `auto_build` | `true` | spawn a detached builder when an episode closes (else run `repogym build`) |
| `verify` | `true` | run tests to discover F2P/P2P |
| `test_timeout` | `900` | seconds per test run |
| `link_dirs` | node_modules, .venv, … | git-ignored dirs symlinked into scratch worktrees |
| `setup_cmd` | `null` | shell command to run in a worktree before tests (e.g. `npm ci`) |
| `capture_tool_inputs` | `summary` | `none` / `summary` / `full` (scrubbed) trajectory detail |
| `exclude_globs` | .env, *.pem, *secret*, … | files dropped from every stored patch |
| `llm.enabled` | `false` | rewrite prompts into clean issue-style problem statements (Anthropic API) |
| `max_diff_lines` | `20000` | skip huge episodes |

Set `REPOGYM_HOME` to relocate the store (e.g. a shared volume or a per-team directory).

### Opting out

Capture is a team decision, but every individual keeps a switch: set `REPOGYM_OPT_OUT=1` in your
shell, or drop a `.repogym-optout` file in a repository, and RepoGym records nothing there.

## Caveats (read before trusting a reward)

- Scratch worktrees reuse the engineer's dependency directories via symlinks. For Python projects that
  are `pip install -e .`'d, RepoGym prepends the worktree to `PYTHONPATH` so the code under test is the
  worktree's, but compiled extensions or generated files that live outside the tree can still leak. For
  publication-grade rigour, run verification in a container built from `head_commit` (`repogym bundle`
  gives you the snapshots).
- Codex only reports when a turn *ends*, so its first turn per thread has an approximate base
  (flagged `base_uncertain`). Subsequent turns are exact. Prefer the experimental hooks
  (`repogym install codex --hooks`) or `repogym wrap` for exact bases.
- Tasks derived from "just do X to file Y" prompts leak their solution in the problem statement.
  Enable `llm.enabled` to rewrite them into issue-style statements and flag leaks
  (`enrichment.leaks_solution_in_prompt`).
- Secret scrubbing is regex-based. Review `exclude_globs` for your stack, and treat the store as
  sensitive: it contains your code.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache-2.0
