"""Command-line interface for local archived-chat cleanup."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from . import __version__
from .cleanup import execute, restore
from .storage import Store, StoreError


def common_options(parser, *, defaults: bool = False):
    default = None if defaults else argparse.SUPPRESS
    parser.add_argument("--home", type=Path, default=default, help="Codex data directory (default CODEX_HOME or ~/.codex)")
    parser.add_argument("--json", action="store_true", default=False if defaults else argparse.SUPPRESS,
                        help="Emit machine-readable JSON")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="codex-kit", description="Manage and prune locally archived Codex chats.")
    root.add_argument("--version", action="version", version=f"codex-kit {__version__}")
    common_options(root, defaults=True)
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List archived chats")
    common_options(listing)
    listing.add_argument("--all", action="store_true", help="Include active chats")
    listing.add_argument("--days", type=int, help="Only chats archived more than N days ago")
    stats = commands.add_parser("stats", help="Count chats and measure rollout/backup storage")
    common_options(stats)
    prune = commands.add_parser("prune", help="Delete chats archived more than N days ago")
    common_options(prune)
    prune.add_argument("--days", type=int, required=True, help="Archive age in 24-hour days")
    prune.add_argument("--archived", action="store_true", help="Select archived chats (the default)")
    prune.add_argument("--dry-run", action="store_true", help="Preview the plan without creating a backup or deleting")
    prune.add_argument("--yes", action="store_true", help="Confirm this cleanup without an interactive prompt")
    backup_options = prune.add_mutually_exclusive_group()
    backup_options.add_argument("--backup-dir", type=Path, help="Backup parent directory")
    backup_options.add_argument("--no-backup", action="store_true", help="Delete without backup; chats cannot be restored")
    prune.add_argument("--codex", default="codex", help="Official Codex executable")
    recovery = commands.add_parser("restore", help="Restore chats from an explicit backup directory")
    common_options(recovery)
    recovery.add_argument("backup", help="Full backup batch directory printed by prune")
    recovery.add_argument("--yes", action="store_true", help="Confirm restore without an interactive prompt")
    return root


def thread_data(thread) -> dict:
    return {"id": thread.id, "title": thread.title, "archived": thread.archived,
            "archived_at": thread.archived_at, "pinned": thread.pinned,
            "rollout_bytes": thread.size, "files": [str(path) for path in thread.files],
            "file_error": thread.file_error}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def safe_text(text: str) -> str:
    return "".join(char if char.isprintable() else " " for char in text)


def print_threads(threads, *, stream=sys.stdout):
    print("ID                                    ARCHIVED AT          SIZE        TITLE", file=stream)
    for thread in threads:
        date = datetime.fromtimestamp(thread.archived_at).astimezone().strftime("%Y-%m-%d %H:%M") if thread.archived_at else "unknown"
        print(f"{thread.id}  {date:<19}  {human_size(thread.size):>10}  {safe_text(thread.title)[:90]}", file=stream)


def confirm(args, description: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty() or args.json:
        raise StoreError("Use --yes to confirm this operation, or --dry-run to preview pruning.")
    print(description, file=sys.stderr)
    print("Type yes to continue: ", end="", file=sys.stderr, flush=True)
    if input().strip() != "yes":
        raise StoreError("Cancelled.")


def run(args) -> dict:
    store = Store(args.home or Path(os.environ.get("CODEX_HOME", "~/.codex")))
    if args.command == "restore":
        confirm(args, "Restore this backup batch. Close Codex before restoring.")
        result = restore(store, args.backup)
        if not args.json:
            print(f"Restored {len(result['restored'])} chats.")
        return result
    if args.command == "prune":
        plan = store.plan(args.days)
        result = {"days": plan.days, "cutoff": plan.cutoff, "roots": plan.roots,
                  "threads": [thread_data(t) for t in plan.threads], "skipped": plan.skipped,
                  "rollout_bytes": sum(t.size for t in plan.threads), "dry_run": args.dry_run, "backup_enabled": not args.no_backup}
        if not args.json:
            print_threads(plan.threads)
            print(f"\nSelected {len(plan.threads)} chats, {human_size(result['rollout_bytes'])} of rollouts.")
            if args.no_backup:
                print("No backup will be created. Deleted chats cannot be restored by codex-kit.")
            else:
                print("Backups retain a copy of deleted data and consume additional space.")
            for item in plan.skipped:
                print(f"Skipped {item['id']}  {item['reason']}")
        if not args.dry_run and plan.threads:
            mode = "without backup; codex-kit cannot restore them" if args.no_backup else "after backing them up"
            confirm(args, f"Permanently delete these {len(plan.threads)} chats {mode}?")
            result.update(execute(store, plan, backup_dir=args.backup_dir, codex=args.codex, backup=not args.no_backup))
            if not args.json:
                print(f"Deleted {len(result['deleted'])} chats.")
                if result["backup"] is not None:
                    print(f"Backup saved to {result['backup']}")
        return result
    threads, _ = store.read()
    if args.command == "list":
        if args.days is not None and args.days <= 0:
            raise StoreError("--days must be a positive integer.")
        cutoff = time.time() - args.days * 86400 if args.days is not None else None
        chosen = [t for t in threads.values() if (args.all or t.archived)
                  and (cutoff is None or (t.archived and t.archived_at is not None and t.archived_at < cutoff))]
        chosen.sort(key=lambda t: (t.archived_at or 0, t.id))
        if not args.json:
            print_threads(chosen)
            print(f"\n{len(chosen)} chats.")
        return {"threads": [thread_data(t) for t in chosen]}
    backup_dir = store.home / "codex-kit" / "backups"
    backup_bytes = sum(p.stat().st_size for p in backup_dir.rglob("*") if p.is_file()) if backup_dir.exists() else 0
    result = {"home": str(store.home), "total": len(threads),
              "archived": sum(t.archived for t in threads.values()),
              "active": sum(not t.archived for t in threads.values()),
              "unknown_archive_time": sum(t.archived and t.archived_at is None for t in threads.values()),
              "rollout_bytes": sum(t.size for t in threads.values()), "backup_bytes": backup_bytes,
              "missing_rollouts": sum(not t.files for t in threads.values())}
    if not args.json:
        print(f"Chats {result['total']}   Archived {result['archived']}   Active {result['active']}")
        print(f"Rollouts {human_size(result['rollout_bytes'])}   Backups {human_size(backup_bytes)}")
        print(f"Unknown archive time {result['unknown_archive_time']}   Missing rollouts {result['missing_rollouts']}")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (StoreError, OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
