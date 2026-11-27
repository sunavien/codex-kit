"""Cleanup through the official Codex deletion command, with optional backups."""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from .storage import Plan, Store, StoreError, connect_readonly

DATABASE_PATTERNS = ("state_*.sqlite", "thread_history_*.sqlite", "goals_*.sqlite",
                     "memories_*.sqlite", "memories_v2_*.sqlite", "logs_*.sqlite")


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


@contextmanager
def operation_lock(home: Path):
    directory = home / "codex-kit"
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / "operation.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StoreError("Another codex-kit operation is running.") from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def check_codex(binary: str) -> str:
    if shutil.which(binary) is None:
        raise StoreError(f"Codex executable not found: {binary}")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    help_result = subprocess.run([binary, "delete", "--help"], capture_output=True, text=True, timeout=30)
    if version.returncode or help_result.returncode or "--force" not in help_result.stdout:
        raise StoreError("This Codex executable does not support delete --force; update Codex first.")
    return version.stdout.strip()


def validate_plan(store: Store, plan: Plan, remaining: set[str]) -> None:
    # Reuse the original cutoff, so chats cannot silently enter the confirmed batch.
    fresh = store.plan(plan.days, now=plan.cutoff + plan.days * 86400)
    fresh_by_id = {t.id: t for t in fresh.threads}
    original = {t.id: t for t in plan.threads}
    for tid in remaining:
        if tid not in fresh_by_id or fresh_by_id[tid].row != original[tid].row:
            raise StoreError(f"Chat changed since preview; run prune again: {tid}")
        if fresh_by_id[tid].files != original[tid].files:
            raise StoreError(f"Rollout files changed since preview: {tid}")
    _, edges = store.read()
    for root in plan.roots:
        if root in remaining:
            expected = plan.subtrees[root] & remaining
            if Store.subtree(root, edges) != expected:
                raise StoreError(f"Subagent relationships changed since preview: {root}")


def create_backup(store: Store, plan: Plan, directory: Path, version: str) -> tuple[Path, dict]:
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    batch = directory.expanduser().resolve() / batch_id
    batch.mkdir(parents=True, mode=0o700)
    os.chmod(batch, 0o700)
    data = {
        "format": 1, "batch_id": batch_id, "home": str(store.home),
        "codex_version": version, "days": plan.days, "cutoff": plan.cutoff,
        "status": "backing_up", "threads": [t.id for t in plan.threads],
        "roots": plan.roots, "deleted": [], "databases": [], "files": [],
    }
    write_json(batch / "manifest.json", data)
    database_paths = sorted({path for pattern in DATABASE_PATTERNS for path in store.home.glob(pattern)})
    for source in database_paths:
        if source.is_symlink():
            raise StoreError(f"Database must not be a symlink: {source}")
        target = batch / "databases" / source.name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        with connect_readonly(source) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreError(f"Database backup failed integrity check: {source.name}")
        os.chmod(target, 0o600)
        data["databases"].append({"name": source.name, "sha256": digest(target)})
    for thread in plan.threads:
        for source in thread.files:
            relative = source.relative_to(store.home)
            target = batch / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = digest(source)
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            if digest(target) != before or digest(source) != before:
                raise StoreError(f"Rollout changed during backup: {source}")
            data["files"].append({"path": str(relative), "sha256": before, "thread_id": thread.id})
    index = store.home / "session_index.jsonl"
    entries = []
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines(keepends=True):
            try:
                if json.loads(line).get("id") in data["threads"]:
                    entries.append(line)
            except (ValueError, AttributeError) as exc:
                raise StoreError("session_index.jsonl contains an invalid record; backup stopped.") from exc
    data["index_entries"] = entries
    data["status"] = "ready"
    write_json(batch / "manifest.json", data)
    return batch, data


def execute(store: Store, plan: Plan, *, backup_dir: Path | None = None,
            codex: str = "codex", backup: bool = True) -> dict:
    if not backup and backup_dir is not None:
        raise StoreError("--no-backup cannot be combined with --backup-dir.")
    if not plan.threads:
        return {"deleted": [], "backup": None, "batch_id": None}
    with operation_lock(store.home):
        version = check_codex(codex)
        validate_plan(store, plan, {t.id for t in plan.threads})
        batch = None
        if backup:
            batch, data = create_backup(store, plan, backup_dir or store.home / "codex-kit" / "backups", version)
        else:
            data = {
                "threads": [thread.id for thread in plan.threads], "deleted": [], "batch_id": None,
                "files": [
                    {"thread_id": thread.id, "path": str(path.relative_to(store.home)), "sha256": digest(path)}
                    for thread in plan.threads for path in thread.files
                ],
            }
        try:
            remaining = set(data["threads"])
            for root in plan.roots:
                if root not in remaining:
                    continue
                validate_plan(store, plan, remaining)
                for item in data["files"]:
                    if item["thread_id"] in remaining and digest(store.home / item["path"]) != item["sha256"]:
                        raise StoreError("Rollout content changed before deletion; run prune again.")
                data["status"] = "deleting"
                data["current_root"] = root
                if batch is not None:
                    write_json(batch / "manifest.json", data)
                env = dict(os.environ, CODEX_HOME=str(store.home))
                result = subprocess.run(
                    [codex, "delete", "-c", "sqlite_home=" + json.dumps(str(store.home)), "--force", root],
                    env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
                )
                if result.returncode:
                    raise StoreError((result.stderr or result.stdout or "Codex delete failed").strip())
                current, _ = store.read()
                affected = plan.subtrees[root] & remaining
                if affected & current.keys() or any(
                    (store.home / item["path"]).exists()
                    for item in data["files"] if item["thread_id"] in affected
                ):
                    raise StoreError("Codex reported success but chat data remains; cleanup stopped.")
                remaining -= affected
                data["deleted"] = sorted(set(data["deleted"]) | affected)
                if batch is not None:
                    write_json(batch / "manifest.json", data)
            data["status"] = "completed"
            data.pop("current_root", None)
            if batch is not None:
                write_json(batch / "manifest.json", data)
        except (Exception, KeyboardInterrupt) as exc:
            data["status"] = "failed"
            data["error"] = str(exc) or "Interrupted"
            if batch is not None:
                write_json(batch / "manifest.json", data)
            if batch is not None:
                detail = f"Backup saved to {batch}"
            else:
                completed = ", ".join(data["deleted"]) or "none"
                detail = f"No backup was created. Confirmed deleted IDs: {completed}. Current root may be partially deleted."
            raise StoreError(f"Cleanup stopped. {detail}\n{data['error']}") from exc
        return {"deleted": data["deleted"], "backup": str(batch) if batch is not None else None, "batch_id": data["batch_id"]}


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def selected_rows(conn: sqlite3.Connection, table: str, ids: set[str]) -> list[dict]:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({quote(table)})")}
    keys = columns & {"thread_id", "session_id", "parent_thread_id", "child_thread_id"}
    if table == "jobs" and "job_key" in columns:
        keys.add("job_key")
    if table == "threads":
        keys = {"id"}
    if not keys:
        return []
    placeholders = ",".join("?" for _ in ids)
    keys = sorted(keys)
    where = " OR ".join(f"{quote(key)} IN ({placeholders})" for key in keys)
    return [dict(row) for row in conn.execute(f"SELECT * FROM {quote(table)} WHERE {where}", list(sorted(ids)) * len(keys))]


def merge_rows(conn: sqlite3.Connection, table: str, rows: list[dict], *, apply: bool) -> None:
    info = list(conn.execute(f"PRAGMA table_info({quote(table)})"))
    columns = [row["name"] for row in info]
    primary = [row["name"] for row in sorted(info, key=lambda r: r["pk"]) if row["pk"]]
    for row in rows:
        if set(row) != set(columns) or not primary:
            raise StoreError(f"Unsupported restore schema for table {table}.")
        where = " AND ".join(f"{quote(key)} IS ?" for key in primary)
        existing = conn.execute(f"SELECT * FROM {quote(table)} WHERE {where}", [row[k] for k in primary]).fetchone()
        if existing is not None:
            if dict(existing) != row:
                raise StoreError(f"Restore conflict in {table}; existing record would be overwritten.")
        elif apply:
            names = ",".join(quote(key) for key in columns)
            placeholders = ",".join("?" for _ in columns)
            conn.execute(f"INSERT INTO {quote(table)} ({names}) VALUES ({placeholders})", [row[k] for k in columns])


def restore(store: Store, backup: str) -> dict:
    batch = Path(backup).expanduser().resolve()
    manifest = batch / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("format") != 1 or data.get("home") != str(store.home):
        raise StoreError("Backup format or CODEX_HOME does not match.")
    if data.get("status") not in {"ready", "deleting", "failed", "completed", "restoring", "restored"}:
        raise StoreError("Backup was not completed; it cannot be restored.")
    ids = set(data["threads"])
    if not ids:
        raise StoreError("Backup contains no chats.")
    with operation_lock(store.home):
        files = []
        for item in data["files"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"sessions", "archived_sessions"}:
                raise StoreError("Unsafe file path in backup manifest.")
            source, target = batch / "files" / relative, store.home / relative
            if not source.resolve().is_relative_to(batch) or not target.resolve().is_relative_to(store.home):
                raise StoreError("Backup file path escapes its directory.")
            if digest(source) != item["sha256"]:
                raise StoreError(f"Backup checksum mismatch: {relative}")
            if target.exists() and digest(target) != item["sha256"]:
                raise StoreError(f"Restore file conflict: {target}")
            files.append((source, target))
        databases = []
        for item in data["databases"]:
            name = item["name"]
            if Path(name).name != name or not any(Path(name).match(p) for p in DATABASE_PATTERNS):
                raise StoreError("Unsafe database path in backup manifest.")
            snapshot, target = batch / "databases" / name, store.home / name
            if snapshot.is_symlink() or target.is_symlink() or digest(snapshot) != item["sha256"]:
                raise StoreError(f"Database backup checksum/path mismatch: {name}")
            if not target.is_file():
                raise StoreError(f"Restore requires the existing database: {name}")
            tables = []
            with connect_readonly(snapshot) as src, connect_readonly(target) as dst:
                for table_row in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    table = table_row["name"]
                    rows = selected_rows(src, table, ids)
                    if rows:
                        merge_rows(dst, table, rows, apply=False)
                        tables.append((table, rows))
            databases.append((target, tables))
        index = store.home / "session_index.jsonl"
        current_index = index.read_text(encoding="utf-8") if index.exists() else ""
        current_lines = current_index.splitlines(keepends=True)
        current_records = [json.loads(line) for line in current_lines]
        entries = []
        for line in data.get("index_entries", []):
            entry = json.loads(line)
            if any(record.get("id") == entry.get("id") and record != entry for record in current_records):
                raise StoreError("Restore conflict in session_index.jsonl; existing name differs.")
            if entry not in current_records:
                entries.append(line)
        # Preflight every conflict before writes. Per-database transactions and identical-row
        # checks make an interrupted restore retryable without replacing unrelated chats.
        data["status"] = "restoring"
        write_json(manifest, data)
        for source, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".codex-kit-", delete=False) as file:
                        temporary = Path(file.name)
                        with source.open("rb") as original:
                            shutil.copyfileobj(original, file)
                        file.flush()
                        os.fsync(file.fileno())
                    # link is an atomic create-if-absent operation, preserving concurrent files.
                    os.link(temporary, target)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        for target, tables in databases:
            with closing(sqlite3.connect(target, timeout=5)) as conn, conn:
                conn.row_factory = sqlite3.Row
                for table, rows in tables:
                    merge_rows(conn, table, rows, apply=True)
        if entries:
            with index.open("a", encoding="utf-8") as file:
                if current_index and not current_index.endswith("\n"):
                    file.write("\n")
                file.writelines(entries)
        data["status"] = "restored"
        write_json(manifest, data)
        return {"restored": sorted(ids), "backup": str(batch)}
