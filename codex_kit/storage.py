"""Read-only access to the supported Codex session catalog."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
import time
from uuid import UUID


class StoreError(Exception):
    """An actionable storage or cleanup error."""


@contextmanager
def connect_readonly(path: Path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


@dataclass
class Thread:
    id: str
    title: str
    archived: bool
    archived_at: int | None
    pinned: bool
    row: dict
    files: list[Path] = field(default_factory=list)
    file_error: str | None = None

    @property
    def size(self) -> int:
        return sum(path.stat().st_size for path in self.files)


@dataclass
class Plan:
    days: int
    cutoff: float
    roots: list[str]
    threads: list[Thread]
    skipped: list[dict]
    subtrees: dict[str, set[str]]


class Store:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        versions = sorted(self.home.glob("state_*.sqlite"))
        self.db = self.home / "state_5.sqlite"
        if versions != [self.db]:
            raise StoreError("Expected exactly state_5.sqlite in CODEX_HOME; missing or unsupported state database.")
        with connect_readonly(self.db) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(threads)")}
            if not {"id", "title", "rollout_path", "archived", "archived_at"} <= columns:
                raise StoreError("Unsupported threads schema; required archive fields are missing.")
            edges = {row["name"] for row in conn.execute("PRAGMA table_info(thread_spawn_edges)")}
            if not {"parent_thread_id", "child_thread_id"} <= edges:
                raise StoreError("Cannot determine the subagent deletion scope; thread_spawn_edges is missing.")

    def read(self) -> tuple[dict[str, Thread], dict[str, set[str]]]:
        with connect_readonly(self.db) as conn:
            conn.execute("BEGIN")
            rows = [dict(row) for row in conn.execute("SELECT * FROM threads")]
            edge_rows = list(conn.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"))
        threads = {}
        for row in rows:
            thread_id = row["id"]
            try:
                if str(UUID(thread_id)) != thread_id:
                    raise ValueError("noncanonical UUID")
            except (ValueError, TypeError, AttributeError) as exc:
                raise StoreError(f"Invalid thread UUID in catalog: {thread_id!r}") from exc
            threads[thread_id] = Thread(
                thread_id, row["title"] or "", bool(row["archived"]),
                row["archived_at"], bool(row.get("is_pinned", False)), row,
            )
        edges: dict[str, set[str]] = {}
        for row in edge_rows:
            edges.setdefault(row["parent_thread_id"], set()).add(row["child_thread_id"])
        self._find_files(threads)
        return threads, edges

    def _find_files(self, threads: dict[str, Thread]) -> None:
        # Native deletion can find a rollout in either directory, including both encodings.
        for directory in ("sessions", "archived_sessions"):
            base = self.home / directory
            if not base.exists():
                continue
            if base.is_symlink():
                raise StoreError(f"Session directory must not be a symlink: {base}")
            for path in base.rglob("rollout-*.jsonl*"):
                name = path.name
                if name.endswith(".jsonl.zst"):
                    stem = name[:-10]
                elif name.endswith(".jsonl"):
                    stem = name[:-6]
                else:
                    continue
                thread = threads.get(stem[-36:])
                if thread is None:
                    continue
                if not path.resolve().is_relative_to(base.resolve()) or path.is_symlink():
                    thread.file_error = "Rollout path escapes the session directory or is a symlink"
                elif path.is_file():
                    thread.files.append(path)
        for thread in threads.values():
            thread.files.sort()
            raw = Path(thread.row["rollout_path"])
            if not raw.is_absolute():
                raw = self.home / raw
            if not any(raw.resolve().is_relative_to((self.home / d).resolve())
                       for d in ("sessions", "archived_sessions")):
                thread.file_error = "Catalog rollout path is outside the session directories"
            elif not any(raw.name.endswith(thread.id + suffix) for suffix in (".jsonl", ".jsonl.zst")):
                thread.file_error = "Catalog rollout filename does not match its thread UUID"
            elif not thread.files:
                thread.file_error = "Rollout file is missing"

    @staticmethod
    def subtree(root: str, edges: dict[str, set[str]]) -> set[str]:
        found = set()
        pending = [root]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(edges.get(current, ()))
        return found

    def plan(self, days: int, *, now: float | None = None) -> Plan:
        if days <= 0:
            raise StoreError("--days must be a positive integer.")
        cutoff = (time.time() if now is None else now) - days * 86400
        threads, edges = self.read()
        candidates = {
            t.id for t in threads.values()
            if t.archived and isinstance(t.archived_at, (int, float)) and t.archived_at < cutoff
        }
        eligible = {tid for tid in candidates if not threads[tid].pinned and not threads[tid].file_error}
        cyclic_nodes = {node for node in edges
                        if any(node in self.subtree(child, edges) for child in edges[node])}
        safe = {}
        skipped = [{"id": t.id, "reason": "Unknown archive time"}
                   for t in threads.values() if t.archived and t.archived_at is None]
        for tid in sorted(candidates):
            subtree = self.subtree(tid, edges)
            if threads[tid].pinned:
                reason = "Pinned chat"
            elif threads[tid].file_error:
                reason = threads[tid].file_error
            elif subtree & cyclic_nodes:
                reason = "Cycle in subagent relationships"
            elif not subtree <= eligible:
                reason = "Descendant is active, recent, pinned, missing, or unknown"
            else:
                safe[tid] = subtree
                continue
            skipped.append({"id": tid, "reason": reason})
        # Select top-level safe subtrees so each descendant is shown once.
        roots = [tid for tid in safe if not any(tid in group - {root} for root, group in safe.items())]
        covered = set().union(*(safe[tid] for tid in roots)) if roots else set()
        ordered = sorted((threads[tid] for tid in covered), key=lambda t: (t.archived_at, t.id))
        return Plan(days, cutoff, roots, ordered, skipped, {tid: safe[tid] for tid in roots})
