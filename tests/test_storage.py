from contextlib import closing
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_kit.storage import Store, StoreError

NOW = 1_800_000_000
DAY = 86400


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex"
        self.home.mkdir()
        self.db = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY, title TEXT, rollout_path TEXT,
                    archived INTEGER, archived_at INTEGER, updated_at INTEGER,
                    is_pinned INTEGER DEFAULT 0
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT, child_thread_id TEXT, status TEXT,
                    PRIMARY KEY(parent_thread_id, child_thread_id)
                );
                CREATE TABLE thread_attachments (
                    id TEXT PRIMARY KEY, thread_id TEXT, payload TEXT
                );
            """)

    def thread(self, *, age=40, archived=True, pinned=False, compressed=False,
               missing=False, title="example", thread_id=None):
        thread_id = thread_id or str(uuid.uuid4())
        directory = "archived_sessions" if archived else "sessions"
        path = self.home / directory / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
        path.parent.mkdir(exist_ok=True)
        actual = Path(str(path) + ".zst") if compressed else path
        if not missing:
            actual.write_bytes(b"compressed fixture" if compressed else b'{"type":"session_meta"}\n')
        archived_at = NOW - age * DAY if age is not None else None
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (thread_id, title, str(path), int(archived), archived_at, NOW, int(pinned)))
        return thread_id

    def edge(self, parent, child):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')", (parent, child))


class StoreTests(StoreFixture):
    def test_age_uses_archive_time_and_strict_boundary(self):
        old = self.thread(age=31)
        self.thread(age=30)
        self.thread(age=29)
        self.thread(age=None)
        self.thread(archived=False)
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([old], [t.id for t in plan.threads])

    def test_compressed_rollout_is_found(self):
        thread_id = self.thread(compressed=True)
        thread = Store(self.home).plan(30, now=NOW).threads[0]
        self.assertEqual(thread_id, thread.id)
        self.assertEqual([".zst"], [p.suffix for p in thread.files])
        self.assertGreater(thread.size, 0)

    def test_parent_with_active_or_recent_descendant_is_skipped(self):
        parent = self.thread()
        active = self.thread(archived=False)
        self.edge(parent, active)
        safe = self.thread()
        recent = self.thread(age=2)
        parent2 = self.thread()
        self.edge(parent2, recent)
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([safe], [t.id for t in plan.threads])
        self.assertEqual({parent, parent2}, {s["id"] for s in plan.skipped})

    def test_nested_delete_is_deduplicated(self):
        parent, child, grandchild = [self.thread() for _ in range(3)]
        self.edge(parent, child)
        self.edge(child, grandchild)
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([parent], plan.roots)
        self.assertEqual({parent, child, grandchild}, {t.id for t in plan.threads})

    def test_missing_and_pinned_and_unknown_descendant_are_skipped(self):
        self.thread(missing=True)
        self.thread(pinned=True)
        parent = self.thread()
        self.edge(parent, str(uuid.uuid4()))
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([], plan.threads)
        self.assertEqual(3, len(plan.skipped))

    def test_cycles_are_reported_and_never_deleted(self):
        a, b = self.thread(), self.thread()
        self.edge(a, b)
        self.edge(b, a)
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([], plan.threads)
        self.assertEqual(2, len(plan.skipped))

    def test_nonexistent_home_is_not_created(self):
        absent = self.home / "absent"
        with self.assertRaises(StoreError):
            Store(absent)
        self.assertFalse(absent.exists())

    def test_unknown_state_version_is_rejected(self):
        self.db.rename(self.home / "state_99.sqlite")
        with self.assertRaises(StoreError):
            Store(self.home)

    def test_symlink_outside_session_directory_is_rejected(self):
        thread_id = self.thread()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            path = Path(conn.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()[0])
        path.unlink()
        outside = self.home.parent / "outside.jsonl"
        outside.write_text("private")
        path.symlink_to(outside)
        self.assertEqual([], Store(self.home).plan(30, now=NOW).threads)

    def test_matching_duplicate_rollouts_are_all_in_backup_scope(self):
        thread_id = self.thread()
        duplicate = self.home / "sessions" / f"rollout-2026-02-01T00-00-00-{thread_id}.jsonl"
        duplicate.parent.mkdir(exist_ok=True)
        duplicate.write_text("duplicate")
        thread = Store(self.home).plan(30, now=NOW).threads[0]
        self.assertEqual(2, len(thread.files))

    def test_invalid_days_rejected(self):
        for days in (0, -1):
            with self.assertRaises(StoreError):
                Store(self.home).plan(days, now=NOW)

    def test_catalog_path_with_another_thread_id_is_skipped(self):
        first = self.thread()
        second = self.thread()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            path = conn.execute("SELECT rollout_path FROM threads WHERE id=?", (second,)).fetchone()[0]
            conn.execute("UPDATE threads SET rollout_path=? WHERE id=?", (path, first))
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([second], [t.id for t in plan.threads])

    def test_unknown_archive_times_are_reported_as_skipped(self):
        tid = self.thread(age=None)
        plan = Store(self.home).plan(30, now=NOW)
        self.assertEqual([], plan.threads)
        self.assertEqual([tid], [item["id"] for item in plan.skipped])
        self.assertIn("archive time", plan.skipped[0]["reason"])

    def test_parent_of_cyclic_descendants_is_also_skipped(self):
        parent, a, b = self.thread(), self.thread(), self.thread()
        self.edge(parent, a)
        self.edge(a, b)
        self.edge(b, a)
        self.assertEqual([], Store(self.home).plan(30, now=NOW).threads)
