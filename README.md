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
repogym remote set s3://acme-repogym/gym   # the organisation's bucket (gs:// and file:// work too)
```

Now just work. Each prompt → stop cycle that changes files becomes an episode; a detached builder
turns it into a task within seconds to minutes (it runs your tests twice) and pushes it to the bucket.
Laptops are only a capture point: set `remote.purge_local` to keep nothing there.

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

env = RepoGymEnv("s3://acme-repogym/gym/tasks/myrepo__fix-divide-by-zero__1a2b3c4d")  # or a local id / dir
obs, info = env.reset()
print(obs["problem_statement"], obs["repo_path"])

obs, r, term, trunc, info = env.step("pytest -q")                       # str == shell command
obs, r, term, trunc, info = env.step({"type": "read", "path": "calc/core.py"})
obs, r, term, trunc, info = env.step({"type": "patch", "diff": my_unified_diff})
obs, reward, term, trunc, info = env.step({"type": "submit"})           # hidden tests run here
env.close()
```

`reward` is 1.0 when every `FAIL_TO_PASS` test passes and no `PASS_TO_PASS` test regresses
(partial credit for a fraction of F2P with no regressions; configurable). On a training box the env
pulls the task from the bucket, clones the repository from the git remote recorded in the task
(blobless partial clone, under `~/.repogym/clones/`), fetches `head_commit` and restores the exact
snapshot commits from the task's thin `snapshots.bundle`. See [`examples/`](examples/) for a
training-side loader, a policy that uses Claude Code itself as the agent, and the gymnasium wrapper.

## The shared gym: S3 / GCS / shared directory

```
laptop A ─┐                                     ┌─ training job:  for task in iter_tasks("s3://acme-repogym/gym"):
laptop B ─┼─ build ─► sync ─► s3://acme-repogym/gym ─┤                       env = RepoGymEnv(task["remote_url"])
laptop C ─┘                                     └─ eval / export:  repogym pull --all ; repogym export --format swebench
```

```bash
repogym remote set s3://acme-repogym/gym   # once per machine; or export REPOGYM_REMOTE=...
repogym sync --purge-local --index         # push anything not yet pushed, drop local copies, rebuild index.jsonl
repogym remote ls --tier verified          # what the organisation has
repogym pull --all                         # fetch everything into ~/.repogym/cache
```

Bucket layout: `<prefix>/tasks/<task_id>/{task.json, problem.md, solution.patch, source.patch, test.patch,
base.patch, trajectory.jsonl, snapshots.bundle}` plus `<prefix>/index.jsonl` (one task per line; rebuild
with `repogym remote index`). A task is ~30 KB, so a million tasks is tens of GB of object storage and
the laptops carry nothing.

Backends: `s3://` uses boto3 when installed (`pip install repogym[s3]`), otherwise the `aws` CLI;
`gs://` uses the `gcloud` CLI; `file://` or a plain path covers NFS and shared drives. Authentication is
whatever those tools already use. MinIO / R2 / Ceph work via `remote.s3_endpoint_url`. Server-side
encryption or a KMS key goes in `remote.s3_extra_args`.

The bucket contains your source code. Treat it like a repo mirror: private bucket, SSE, least-privilege
IAM (writers need `PutObject` on `tasks/*`, trainers need `GetObject`/`ListBucket`).

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
| `remote.url` | `null` | `s3://bucket/prefix`, `gs://bucket/prefix` or `file:///path` (`REPOGYM_REMOTE` overrides) |
| `remote.auto_sync` | `true` | push each task as soon as it is built |
| `remote.purge_local` | `false` | delete the local task directory once pushed |
| `remote.tiers` | `null` | only push these tiers (e.g. `["verified","suite"]`) |
| `remote.aws_profile` / `s3_endpoint_url` / `s3_extra_args` | — | AWS profile, custom S3 endpoint, boto3 `ExtraArgs` (SSE/KMS) |
| `max_file_size_mb` | `5` | untracked files above this never enter a snapshot |
| `snapshot_retention_days` | `7` | unpin snapshot commits this long after a task is built (`0` = immediately, `-1` = keep) |
| `event_log` / `log_max_mb` | `true` / `5` | hook audit log on/off and rotation size for all logs |

Set `REPOGYM_HOME` to relocate the store (e.g. a shared volume or a per-team directory).

## Local footprint

With a remote configured and `purge_local` on, a laptop keeps only episode records (~5 KB each) and
transient scratch worktrees. Without a remote, RepoGym still does not copy repositories. Measured on
the test fixture:

| what | where | size |
|---|---|---|
| one task (patches, problem, trajectory, metadata) | `~/.repogym/tasks/<id>/` | ~30 KB |
| one episode record + audit events | `~/.repogym/repos/<id>/` | ~5 KB |
| snapshot objects for a typical turn | the repo's own `.git` | a few KB (unchanged files dedupe against HEAD) |
| scratch worktree during a build | `$TMPDIR` | one checkout of the repo, deleted after the run |

Two things are actively controlled:

- **Big untracked files.** A build artifact or dataset that is not gitignored would otherwise be
  committed into a snapshot. Files over `max_file_size_mb` are excluded (recorded as `skipped_large`).
- **Pinned snapshots.** Snapshot commits are pinned under `refs/repogym/` so the builder can diff them.
  After a task is built they are no longer needed: the task directory holds `head_commit`, `base.patch`
  and the solution/test patches, and `RepoGymEnv` reconstructs the base state from those. The builder
  unpins finished episodes after `snapshot_retention_days`; git's normal gc then reclaims the objects.

```bash
repogym status                 # shows store size and pinned snapshot bytes per repo
repogym gc --dry-run           # what would be unpinned now
repogym gc --days 0 --git-gc   # unpin everything finished and let git gc reclaim (default 2-week expiry)
repogym gc --prune-now         # reclaim immediately
```

Only one builder runs at a time (a lock in `~/.repogym`), so at most one scratch worktree exists.
Logs and the event audit trail rotate at `log_max_mb`. For a shared team store, point `REPOGYM_HOME`
at a network volume; the per-machine cost is then just the transient worktree.

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
