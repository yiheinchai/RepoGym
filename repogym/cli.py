"""RepoGym command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, config
from .store import Store


def _store() -> Store:
    return Store(config.ensure_home())


def cmd_init(args) -> int:
    home = config.ensure_home()
    print(f"RepoGym home: {home}")
    print("Next: `repogym install claude-code` and/or `repogym install codex`.")
    return 0


def cmd_install(args) -> int:
    from .adapters import claude_code, codex
    if args.agent == "claude-code":
        path = Path(args.path) if args.path else claude_code.settings_path(args.scope, Path(args.project) if args.project else None)
        cmd = args.command or _default_hook_command()
        claude_code.install(path, command=cmd, timeout=args.timeout)
        print(f"Installed Claude Code hooks in {path}")
        print(f"  hook command: {cmd}")
        print("  events: " + ", ".join(claude_code.HOOK_EVENTS))
    elif args.agent == "codex":
        if args.hooks:
            p = codex.install_hooks(Path(args.path) if args.path else None)
            print(f"Installed experimental Codex hooks in {p}")
        else:
            p = codex.install_notify(Path(args.path) if args.path else None)
            print(f"Installed Codex notify handler in {p}")
    config.ensure_home()
    return 0


def cmd_uninstall(args) -> int:
    from .adapters import claude_code, codex
    if args.agent == "claude-code":
        path = Path(args.path) if args.path else claude_code.settings_path(args.scope, Path(args.project) if args.project else None)
        claude_code.uninstall(path)
        print(f"Removed RepoGym hooks from {path}")
    else:
        p = codex.uninstall_notify(Path(args.path) if args.path else None)
        print(f"Removed RepoGym notify handler from {p}")
    return 0


def _default_hook_command() -> str:
    # Prefer the console script when it is on PATH, else an absolute python -m invocation so
    # hooks keep working in shells where PATH differs from the install environment.
    from shutil import which
    if which("repogym"):
        return "repogym hook"
    return f"{sys.executable} -m repogym hook"


def cmd_hook(args) -> int:
    from .adapters import claude_code
    if args.agent and args.agent != "claude-code":
        # Codex hooks use the same payload schema; tag the episode with the right agent.
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return 0
        payload["_agent"] = args.agent
        try:
            claude_code.handle(payload)
        except Exception:
            pass
        return 0
    return claude_code.main_stdin()


def cmd_codex_notify(args) -> int:
    from .adapters import codex
    return codex.main_argv(args.payload)


def cmd_wrap(args) -> int:
    from .adapters import wrap
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: repogym wrap [--prompt TEXT] -- <command...>", file=sys.stderr)
        return 2
    return wrap.run_wrapped(cmd, prompt=args.prompt, agent=args.agent)


def cmd_snapshot(args) -> int:
    from .capture import Capture
    cap = Capture(_store())
    repo = cap.resolve_repo(Path(args.repo or os.getcwd()))
    if repo is None:
        print("not inside a git repository (or opted out)", file=sys.stderr)
        return 1
    session = args.session or "manual"
    if args.action == "begin":
        ep = cap.begin(repo, "manual", session, prompt=args.prompt)
        print(ep.id)
    else:
        ep = cap.end(repo, session, reason="manual", enqueue=not args.no_build)
        print(ep.id if ep else "no open episode")
    return 0


def cmd_build(args) -> int:
    from . import builder
    store = _store()
    cfg = config.load_config()
    llm = True if args.llm else (False if args.no_llm else None)
    verify = False if args.no_verify else None
    if args.episode:
        ep = store.find_episode(args.episode)
        if ep is None:
            print(f"episode not found: {args.episode}", file=sys.stderr)
            return 1
        if args.force and ep.status in ("built", "skipped", "failed"):
            ep.status = "closed"
        task = builder.build_episode(ep, store, cfg, llm=llm, verify=verify, quiet=args.quiet)
        return 0 if task else 1
    if args.merge_session:
        repo_id, _, session_id = args.merge_session.partition(":")
        merged = builder.merge_session(store, repo_id, session_id)
        if merged is None:
            print("nothing to merge", file=sys.stderr)
            return 1
        task = builder.build_episode(merged, store, cfg, llm=llm, verify=verify, quiet=args.quiet)
        return 0 if task else 1
    if args.all:
        n = builder.build_all_closed(store, cfg, quiet=args.quiet, llm=llm, verify=verify)
        if not args.quiet:
            print(f"processed {n} episode(s)")
        return 0
    if args.daemon:
        if not args.quiet:
            print("RepoGym worker running; Ctrl-C to stop")
        try:
            while True:
                n = builder.drain_queue(store, cfg, quiet=args.quiet, llm=llm, verify=verify)
                if n == 0:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    # One builder at a time: bounds CPU and the transient disk used by scratch worktrees. Later
    # spawns wait for the lock, then drain whatever is left.
    from .util import file_lock
    try:
        with file_lock(store.root / "builder.lock", timeout=6 * 3600):
            n = builder.drain_queue(store, cfg, quiet=args.quiet, llm=llm, verify=verify)
    except TimeoutError:
        return 0
    if not args.quiet:
        print(f"built {n} queued episode(s)")
    return 0


def cmd_list(args) -> int:
    store = _store()
    if args.what == "repos":
        for r in store.repos():
            print(f"{r['id']}  {r['name']:<24} {r['path']}")
        return 0
    if args.what == "episodes":
        for ep in store.episodes(status=args.status):
            prompt = (ep.prompts[0] if ep.prompts else "").splitlines()[0] if ep.prompts and ep.prompts[0] else ""
            print(f"{ep.id}  {ep.status:<8} {ep.agent:<12} {ep.repo_id}  tools={len(ep.tools):<4} {prompt[:60]}")
        return 0
    tasks = store.tasks()
    if args.tier:
        tasks = [t for t in tasks if t.get("tier") == args.tier]
    if args.json:
        print(json.dumps(tasks, indent=2))
        return 0
    for t in tasks:
        print(f"{t['id']}\n    tier={t['tier']:<10} lang={t.get('language'):<10} runner={t.get('runner')} "
              f"F2P={len(t.get('FAIL_TO_PASS', []))} P2P={len(t.get('PASS_TO_PASS', []))} "
              f"lines={t.get('diff_lines')} agent={(t.get('source') or {}).get('agent')}")
    return 0


def cmd_show(args) -> int:
    store = _store()
    task = store.load_task(args.task)
    tdir = store.task_dir(task["id"]) if not Path(args.task).is_dir() else Path(args.task)
    if args.json:
        print(json.dumps(task, indent=2))
        return 0
    print(f"# {task['title']}  [{task['tier']}]")
    print(f"id: {task['id']}\nrepo: {task['repo']['name']} ({task['repo'].get('path')})")
    print(f"language: {task.get('language')}  runner: {task.get('runner')}  diff lines: {task.get('diff_lines')}")
    print(f"base: {task['base_commit'][:12]} (head {str(task.get('head_commit'))[:12]}, dirty={task.get('base_dirty')})")
    print(f"FAIL_TO_PASS ({len(task['FAIL_TO_PASS'])}):")
    for t in task["FAIL_TO_PASS"][:20]:
        print(f"  - {t}")
    print(f"PASS_TO_PASS: {len(task['PASS_TO_PASS'])}")
    note = (task.get("verification") or {}).get("note")
    if note:
        print(f"note: {note}")
    print("\n## Problem statement\n")
    print((tdir / "problem.md").read_text(encoding="utf-8"))
    if args.patch:
        print("\n## Solution patch\n")
        print((tdir / "solution.patch").read_text(encoding="utf-8"))
    return 0


def cmd_verify(args) -> int:
    from .verify import verify_patch
    store = _store()
    cfg = config.load_config()
    task = store.load_task(args.task)
    tdir = Path(args.task) if Path(args.task).is_dir() else store.task_dir(task["id"])
    if args.gold:
        src = tdir / "source.patch"
        patch = src.read_text(encoding="utf-8") if src.exists() else (tdir / "solution.patch").read_text(encoding="utf-8")
    elif args.patch:
        patch = Path(args.patch).read_text(encoding="utf-8")
    else:
        patch = sys.stdin.read()
    info = verify_patch(task, tdir, patch, repo=Path(args.repo) if args.repo else None, cfg=cfg,
                        timeout=args.timeout, store_root=store.root)
    if args.json:
        print(json.dumps(info, indent=2, default=str))
    else:
        print(f"tier={info['tier']} success={info.get('success')} reward={info.get('reward')}")
        if "fail_to_pass_total" in info:
            print(f"FAIL_TO_PASS {info['fail_to_pass_passed']}/{info['fail_to_pass_total']}  "
                  f"PASS_TO_PASS failures: {len(info.get('pass_to_pass_failed', []))}")
        if not info.get("patch_applied", True):
            print("patch did not apply:\n" + str(info.get("apply_error")))
    return 0 if info.get("success") else 1


def cmd_export(args) -> int:
    from .export import export
    store = _store()
    n = export(store, Path(args.out), fmt=args.format, tiers=args.tier or None, task_ids=args.task or None)
    print(f"exported {n} task(s) to {args.out}")
    return 0


def cmd_bundle(args) -> int:
    from .export import bundle
    store = _store()
    out = bundle(store, args.task, Path(args.out))
    print(f"wrote {out}")
    return 0


def cmd_status(args) -> int:
    store = _store()
    cfg = config.load_config()
    eps = list(store.episodes())
    by_status = {}
    for e in eps:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    tasks = store.tasks()
    by_tier = {}
    for t in tasks:
        by_tier[t.get("tier")] = by_tier.get(t.get("tier"), 0) + 1
    print(f"home:      {store.root}")
    print(f"repos:     {len(store.repos())}")
    print(f"episodes:  {len(eps)}  {by_status}")
    print(f"queue:     {store.queue_size()} pending")
    print(f"tasks:     {len(tasks)}  {by_tier}")
    print(f"auto_build={cfg.get('auto_build')} verify={cfg.get('verify')} llm={cfg.get('llm', {}).get('enabled')}")
    from .gc import disk_usage, human
    du = disk_usage(store)
    print(f"disk:      store {human(du['store_bytes'])} (tasks {human(du['tasks_bytes'])}); "
          f"pinned snapshot objects in repos {human(du['pinned_bytes_total'])}")
    for r in du["repos"]:
        if r.get("missing"):
            print(f"           {r['name']}: repo missing")
        else:
            print(f"           {r['name']}: {r['snapshot_refs']} snapshot refs, {human(r['snapshot_bytes'])}")
    return 0


def cmd_gc(args) -> int:
    from . import gitsnap
    from .gc import disk_usage, human, prune_snapshots, rotate_logs
    store = _store()
    removed = prune_snapshots(store, days=args.days, dry_run=args.dry_run)
    verb = "would unpin" if args.dry_run else "unpinned"
    print(f"{verb} {len(removed)} snapshot ref(s)")
    if not args.dry_run:
        rotate_logs(store)
    if args.git_gc or args.prune_now:
        for r in store.repos():
            repo = Path(r["path"])
            if repo.exists() and not args.dry_run:
                gitsnap.garbage_collect(repo, prune_now=args.prune_now)
                print(f"git gc {'--prune=now ' if args.prune_now else ''}in {repo}")
    du = disk_usage(store)
    print(f"store {human(du['store_bytes'])}; pinned snapshot objects {human(du['pinned_bytes_total'])}")
    return 0


def cmd_config(args) -> int:
    cfg = config.load_config()
    if args.action == "get":
        val = cfg
        for part in (args.key or "").split(".") if args.key else []:
            val = val.get(part) if isinstance(val, dict) else None
        print(json.dumps(val, indent=2))
        return 0
    if args.action == "set":
        keys = args.key.split(".")
        target = cfg
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        raw = args.value
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        target[keys[-1]] = value
        config.save_config(cfg)
        print(f"{args.key} = {json.dumps(value)}")
        return 0
    print(json.dumps(cfg, indent=2))
    return 0


def cmd_remote(args) -> int:
    from . import remote as rmod
    cfg = config.load_config()
    if args.action == "set":
        cfg.setdefault("remote", {})["url"] = args.url
        config.save_config(cfg)
        print(f"remote = {args.url}")
        return 0
    if args.action == "index":
        r = rmod.open_remote(args.url)
        n = r.rebuild_index()
        print(f"indexed {n} task(s) in {r.url}")
        return 0
    if args.action == "ls":
        for m in rmod.iter_tasks(args.url, tiers=args.tier or None, use_index=not args.no_index):
            print(f"{m['id']}  tier={m.get('tier'):<10} lang={m.get('language')} F2P={len(m.get('FAIL_TO_PASS', []))}")
        return 0
    # status
    url = args.url or os.environ.get("REPOGYM_REMOTE") or (cfg.get("remote") or {}).get("url")
    print(f"remote:      {url or '(none)'}")
    rc = cfg.get("remote") or {}
    print(f"auto_sync={rc.get('auto_sync')} purge_local={rc.get('purge_local')} tiers={rc.get('tiers')}")
    if url:
        store = _store()
        local = store.tasks()
        synced = sum(1 for t in local if rmod.is_synced(store.root, t["id"]))
        print(f"local tasks: {len(local)} ({synced} synced)")
        try:
            r = rmod.open_remote(url)
            print(f"remote tasks: {len(r.list_task_ids())}")
        except Exception as e:  # noqa: BLE001
            print(f"remote unreachable: {e}")
    return 0


def cmd_sync(args) -> int:
    from .remote import sync_all, open_remote
    store = _store()
    remote = open_remote(args.remote)
    n = sync_all(store, remote, purge_local=True if args.purge_local else None, force=args.force,
                 tiers=args.tier or None, quiet=args.quiet)
    if args.index:
        remote.rebuild_index()
    print(f"pushed {n} task(s) to {remote.url}")
    return 0


def cmd_pull(args) -> int:
    from .remote import open_remote, pull_task
    store = _store()
    remote = open_remote(args.remote)
    ids = list(args.task)
    if args.all:
        ids = remote.list_task_ids()
    n = 0
    for tid in ids:
        dest = pull_task(store, tid, remote, refresh=args.refresh)
        n += 1
        if not args.quiet:
            print(f"{tid} -> {dest}")
    print(f"pulled {n} task(s) from {remote.url}")
    return 0


def cmd_mirror(args) -> int:
    from . import gitsnap
    from .mirror import mirror_status, push_mirror, restore_mirror
    from .remote import open_remote
    store = _store()
    remote = open_remote(args.remote)
    if args.action == "push":
        repo = gitsnap.find_repo_root(Path(args.repo or os.getcwd()))
        if repo is None:
            print("not a git repository", file=sys.stderr)
            return 1
        meta = store.register_repo(repo, remote=gitsnap.remote_url(repo))
        rec = push_mirror(remote, repo, meta["id"], meta["name"], meta.get("remote"), force_full=args.full)
        if rec is None:
            print(f"{meta['name']}: mirror already up to date")
        else:
            print(f"{meta['name']}: pushed {rec['name']} ({rec['bytes'] / 1024:.0f} KB, {'thin' if rec['thin'] else 'full'})")
        return 0
    if args.action == "restore":
        from .clones import clone_root
        from .mirror import get_manifest
        repo_id, name = None, None
        for r in store.repos():
            if r["id"] == args.repo or r["name"] == args.repo:
                repo_id, name = r["id"], r["name"]
        if repo_id is None:
            # Fresh machine: resolve the name from the manifests in the bucket.
            for rid in remote.list_dirs("repos/"):
                m = get_manifest(remote, rid)
                if m and (rid == args.repo or m.get("name") == args.repo):
                    repo_id, name = rid, m.get("name") or rid
                    break
        if repo_id is None:
            print(f"unknown repo: {args.repo}", file=sys.stderr)
            return 1
        dest = restore_mirror(remote, repo_id, clone_root(store.root) / name)
        print(f"restored to {dest}" if dest else "no mirror for that repo in the remote")
        return 0 if dest else 1
    # status
    ids = {r["id"]: r["name"] for r in store.repos()}
    for rid in sorted(set(ids) | set(remote.list_dirs("repos/"))):
        st = mirror_status(remote, rid)
        if st:
            print(f"{rid}  {st['name'] or ids.get(rid, ''):<24} bundles={st['bundles']} "
                  f"size={st['bytes'] / 1024 / 1024:.1f} MB covered_heads={st['covered']} updated={st['updated_at']}")
        else:
            print(f"{rid}  {ids.get(rid, ''):<24} (not mirrored)")
    return 0


def cmd_delete(args) -> int:
    store = _store()
    for tid in args.task:
        store.delete_task(tid)
        print(f"deleted {tid}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repogym", description="Turn coding-agent sessions into a private RL gym.")
    p.add_argument("--version", action="version", version=f"repogym {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the RepoGym home directory")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("install", help="install capture hooks into a coding agent")
    s.add_argument("agent", choices=["claude-code", "codex"])
    s.add_argument("--scope", choices=["user", "project", "local"], default="user",
                   help="Claude Code settings file to edit (default: user)")
    s.add_argument("--project", help="project directory for --scope project/local")
    s.add_argument("--path", help="explicit settings/config file path")
    s.add_argument("--command", help="hook command to register (default: auto)")
    s.add_argument("--timeout", type=int, default=120, help="hook timeout in seconds")
    s.add_argument("--hooks", action="store_true", help="codex: install experimental hooks.json instead of notify")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("uninstall", help="remove capture hooks")
    s.add_argument("agent", choices=["claude-code", "codex"])
    s.add_argument("--scope", choices=["user", "project", "local"], default="user")
    s.add_argument("--project")
    s.add_argument("--path")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("hook", help="(internal) Claude Code hook entry point; reads JSON from stdin")
    s.add_argument("--agent", default=None)
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("codex-notify", help="(internal) Codex notify entry point")
    s.add_argument("payload", nargs="*")
    s.set_defaults(fn=cmd_codex_notify)

    s = sub.add_parser("wrap", help="run any agent command with before/after capture")
    s.add_argument("--prompt", help="task prompt (default: inferred from the command)")
    s.add_argument("--agent", help="agent label")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_wrap)

    s = sub.add_parser("snapshot", help="manually begin/end an episode in the current repo")
    s.add_argument("action", choices=["begin", "end"])
    s.add_argument("--repo")
    s.add_argument("--session")
    s.add_argument("--prompt")
    s.add_argument("--no-build", action="store_true")
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("build", help="turn captured episodes into tasks")
    s.add_argument("--queue", action="store_true", help="drain the build queue (default)")
    s.add_argument("--all", action="store_true", help="build every closed episode")
    s.add_argument("--episode", help="build one episode by id")
    s.add_argument("--merge-session", metavar="REPO_ID:SESSION_ID", help="build one task from a whole session")
    s.add_argument("--force", action="store_true", help="rebuild even if already built/skipped")
    s.add_argument("--daemon", action="store_true", help="keep running and drain the queue continuously")
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--llm", action="store_true", help="use an LLM to write the problem statement")
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--no-verify", action="store_true", help="skip running tests")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("list", help="list repos, episodes or tasks")
    s.add_argument("what", nargs="?", choices=["tasks", "episodes", "repos"], default="tasks")
    s.add_argument("--status")
    s.add_argument("--tier")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="show a task")
    s.add_argument("task")
    s.add_argument("--patch", action="store_true", help="include the reference patch")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("verify", help="grade a patch against a task")
    s.add_argument("task")
    s.add_argument("--patch", help="patch file (default: stdin)")
    s.add_argument("--gold", action="store_true", help="verify the reference solution itself")
    s.add_argument("--repo", help="local clone to use")
    s.add_argument("--timeout", type=float)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("export", help="export tasks (swebench | jsonl)")
    s.add_argument("--format", choices=["swebench", "jsonl"], default="swebench")
    s.add_argument("--out", "-o", required=True)
    s.add_argument("--tier", action="append", help="only these tiers (repeatable)")
    s.add_argument("--task", action="append", help="only these task ids (repeatable)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("bundle", help="write a git bundle with a task's snapshot commits")
    s.add_argument("task")
    s.add_argument("--out", "-o", required=True)
    s.set_defaults(fn=cmd_bundle)

    s = sub.add_parser("status", help="overview of captured data")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("remote", help="configure/inspect the shared task store (S3, GCS, shared dir)")
    s.add_argument("action", nargs="?", choices=["status", "set", "ls", "index"], default="status")
    s.add_argument("url", nargs="?", help="s3://bucket/prefix | gs://bucket/prefix | file:///path")
    s.add_argument("--tier", action="append")
    s.add_argument("--no-index", action="store_true", help="ls: list objects instead of reading index.jsonl")
    s.set_defaults(fn=cmd_remote)

    s = sub.add_parser("sync", help="push local tasks to the remote")
    s.add_argument("--remote", help="override the configured remote URL")
    s.add_argument("--purge-local", action="store_true", help="delete local task dirs after upload")
    s.add_argument("--force", action="store_true", help="re-upload tasks already marked synced")
    s.add_argument("--tier", action="append")
    s.add_argument("--index", action="store_true", help="rebuild index.jsonl afterwards")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("pull", help="fetch tasks from the remote into the local cache")
    s.add_argument("task", nargs="*")
    s.add_argument("--all", action="store_true")
    s.add_argument("--remote")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("mirror", help="repository history in the bucket: status | push | restore")
    s.add_argument("action", nargs="?", choices=["status", "push", "restore"], default="status")
    s.add_argument("repo", nargs="?", help="push: repo path (default cwd); restore: repo id or name")
    s.add_argument("--remote")
    s.add_argument("--full", action="store_true", help="push: force a fresh full bundle")
    s.set_defaults(fn=cmd_mirror)

    s = sub.add_parser("gc", help="unpin old snapshots, rotate logs, report disk usage")
    s.add_argument("--days", type=int, help="retention in days (default: config snapshot_retention_days)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--git-gc", action="store_true", help="also run `git gc` in each captured repo")
    s.add_argument("--prune-now", action="store_true", help="git gc --prune=now (reclaims space immediately)")
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("config", help="get/set configuration")
    s.add_argument("action", nargs="?", choices=["get", "set"], default=None)
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("delete", help="delete task(s)")
    s.add_argument("task", nargs="+")
    s.set_defaults(fn=cmd_delete)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
