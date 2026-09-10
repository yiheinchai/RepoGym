# RepoGym architecture

## Design goals

1. **Never slow down or alter the engineer's work.** Capture runs inside editor hooks; each hook does
   O(git add) work and exits 0 regardless of errors. The working tree, index and HEAD are never
   modified. Expensive work happens in a detached process.
2. **Rewards must be executable.** A task is only as good as its verifier. RepoGym reproduces the
   SWE-bench recipe (FAIL_TO_PASS / PASS_TO_PASS discovered by running tests before and after the
   reference change) and is honest about tasks where it can't (`tier`).
3. **Data stays with its owner.** Local store, regex + path-based redaction, opt-out switches, optional
   and explicit LLM usage.
4. **Interoperate.** SWE-bench JSONL export, git bundles, a gym/gymnasium interface.

## Modules

| module | responsibility |
|---|---|
| `gitsnap` | temp-index snapshots, diffs, patch application, scratch worktrees, dependency symlinks, bundles |
| `capture` | episode state machine (`begin` / `tool` / `end`), scrubbing at ingest, detached builder spawn |
| `adapters.claude_code` | stdin hook payload → capture; settings.json install/uninstall |
| `adapters.codex` | `notify` payload → capture (turn chaining); config.toml install; experimental hooks.json |
| `adapters.wrap` | before/after capture around any command |
| `store` | `~/.repogym` layout: repos, episodes, events, queue (atomic claim by rename), tasks |
| `testrunners` | test-path heuristics, test-command classification, runner detection, machine-readable invocations, parsers (junit/jest/vitest/go/cargo/rspec) |
| `builder` | episode → task: diff split, verification, tiering, task directory |
| `deps` | lockfile detection, per-(repo, lockfile hash) provisioning cache, linking into worktrees |
| `verify` | materialize base (snapshot or `head_commit` + `base.patch`), apply candidate + hidden tests, run, score |
| `env` | `RepoGymEnv` (+ gymnasium wrapper) |
| `export` | SWE-bench / full JSONL, git bundle |
| `llm` | optional problem-statement rewriting (Anthropic SDK, structured output) |
| `remote` | object-store backends (S3 via boto3/aws CLI, GCS via gcloud, directory), task push/pull/index, sync markers |
| `mirror` | per-repo git history in the bucket: full bundle then thin increments, manifest of covered heads, restore |
| `clones` | local clones under `~/.repogym/clones`: restore from the bucket mirror, else clone the git remote |
| `gc` | snapshot unpinning by retention, log rotation, disk accounting |
| `cli` | `repogym …` |

## Episode lifecycle (Claude Code)

```
SessionStart      register repo
UserPromptSubmit  open_episode(session)? append prompt : new episode + base snapshot
PostToolUse       append {tool, command|path, exit_code}; classify test commands
Stop              final snapshot; status=closed (or skipped when trees are equal); enqueue; spawn builder
SessionEnd        close anything still open
```

Concurrency: hooks for parallel tool calls can overlap, so every read-modify-write of an episode holds
a per-repo `flock`. The queue uses rename-to-claim so several builders can run at once.

## Build algorithm

```
files      = changed(base, final) - exclude_globs
test_files = [f for f in files if is_test_path(f)]
patches    = diff(files), diff(test_files), diff(files - test_files)
runner     = classify(engineer's Bash test commands) or detect(project files)
targets    = test_files or paths from the engineer's last test command or whole suite

worktree(base):
    link node_modules/.venv/...
    apply test.patch      ; before = run(runner, targets)
    apply source.patch    ; after  = run(runner, targets)

F2P = {t : after[t]==pass and before.get(t, fail)==fail}
P2P = {t : after[t]==pass and before[t]==pass}
tier = verified if F2P else suite if (after.rc==0 and before.rc!=0) else unverified
```

Deleted test files are dropped from `targets` per run so a run never aborts on a missing path. Tests
that don't exist before (new files, collection errors) count as failing before, which is what makes
"add a test for the new behaviour" episodes verifiable.

## Reproducibility

Snapshot commits are synthetic (their parent is the real HEAD). To make tasks portable RepoGym stores
`head_commit` plus `base.patch` (= diff HEAD→base snapshot, i.e. the engineer's uncommitted work when
they asked for help). `verify.materialize` first tries the snapshot commit, then falls back to
`head_commit + base.patch`. `repogym bundle` exports the snapshot refs as a git bundle for machines
that only have the upstream history.

## Portability: how a task runs on a machine that never saw the engineer's checkout

A task references two commits: `head_commit` (real) and `base_commit` (synthetic snapshot). Where the
history comes from, in order (`clones.ensure_repo`):

1. an existing local clone or the engineer's checkout;
2. **the bucket mirror** (`repos/<repo_id>/`): a full `git bundle --all` made on first sync, then thin
   bundles built with `^<previously covered heads>` so only new objects are uploaded. `--all` includes
   `refs/repogym/snapshots/*`, so the exact snapshot commits are mirrored while they are pinned. Restore
   = `git fetch <bundle>` in manifest order into a local clone; a state file records applied bundles;
3. the git remote recorded in the task, as a fallback when no mirror exists.

Within a restored repo, `verify.materialize` uses the snapshot commit if present, else `snapshots.bundle`
(thin bundle with `head_commit` as prerequisite), else `head_commit` + `base.patch`.

Consequence: with mirroring on (default), the bucket alone reproduces every task. Renames, deletions and
history rewrites upstream do not invalidate the gym. The mirror costs roughly the repository's `.git`
size once, plus deltas; tasks stay tiny. What the mirror does *not* carry is the toolchain (interpreter,
installed packages). Tasks record detected versions under `toolchain` so a container image can be built
to match; that is the next layer.

## Dependencies

`deps.detect` runs on the base checkout at build time and the result is stored in `task.json` under
`deps`. `verify.materialize` (used by the builder, the verifier and the env) calls `deps.ensure`, which
installs into `~/.repogym/deps/<repo_id>/<manager>-<lockhash>/` under a file lock and symlinks the
resulting directory into the worktree. Python venvs are created directly in the cache (they are not
relocatable); node_modules / vendor are installed in the worktree and moved. Go and cargo only warm
their global caches. A `.failed` marker with the install log stops repeated attempts; `repogym deps`
retries explicitly. The engineer's own dirs, when present, win over provisioning.

## Reward

`verify.score` mirrors SWE-bench: success ⇔ all F2P pass ∧ all P2P pass. Reward = 1.0 on success,
else (fraction of F2P passed) when there are no P2P regressions and partial credit is enabled, else 0.
Unverified tasks return `reward=None` with `similarity` (content-line similarity to the reference
patch) for advisory use only; do not train on it as if it were ground truth.

## Threat model for the data

The store contains source code and prompts. Redaction targets *accidental* leakage of credentials into
prompts/commands/diffs, plus wholesale exclusion of secret-shaped files. It is not DLP. Keep
`REPOGYM_HOME` on an encrypted, access-controlled volume and review `exclude_globs` for your stack.
