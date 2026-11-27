from contextlib import closing
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_kit.cleanup import digest, execute, restore
from codex_kit.storage import Store, StoreError
from tests.test_storage import NOW, StoreFixture


class CleanupTests(StoreFixture):
    def native_delete(self, args, **kwargs):
        from subprocess import CompletedProcess
        root = args[-1]
        threads, edges = Store(self.home).read()
        ids = Store.subtree(root, edges)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for tid in ids:
                for file in threads[tid].files:
                    file.unlink()
                conn.execute("DELETE FROM thread_attachments WHERE thread_id=?", (tid,))
                conn.execute("DELETE FROM threads WHERE id=?", (tid,))
                conn.execute("DELETE FROM thread_spawn_edges WHERE parent_thread_id=? OR child_thread_id=?", (tid, tid))
        return CompletedProcess(args, 0, "Deleted", "")

    def run_plan(self, plan, **kwargs):
        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=self.native_delete):
            return execute(Store(self.home), plan, backup_dir=self.home.parent / "backups", **kwargs)

    def test_backup_delete_restore_preserves_unrelated_new_chats(self):
        parent, child = self.thread(), self.thread(compressed=True)
        self.edge(parent, child)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO thread_attachments VALUES ('a', ?, 'payload')", (parent,))
        originals = {p: p.read_bytes() for t in Store(self.home).read()[0].values() for p in t.files}
        index = self.home / "session_index.jsonl"
        line = json.dumps({"id": parent, "thread_name": "parent"}) + "\n"
        index.write_text(line)
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        self.assertEqual(2, len(result["deleted"]))
        self.assertEqual({}, Store(self.home).read()[0])
        unrelated = self.thread(archived=False)
        index.write_text(json.dumps({"id": unrelated, "thread_name": "new"}) + "\n")
        restore(Store(self.home), result["backup"])
        threads, edges = Store(self.home).read()
        self.assertEqual({parent, child, unrelated}, set(threads))
        self.assertEqual({child}, edges[parent])
        for path, content in originals.items():
            self.assertEqual(content, path.read_bytes())
        self.assertIn(line, index.read_text())
        self.assertIn(unrelated, index.read_text())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual('payload', conn.execute("SELECT payload FROM thread_attachments WHERE id='a'").fetchone()[0])
        restore(Store(self.home), result["backup"])
        self.assertEqual(3, len(Store(self.home).read()[0]))

    def test_backup_restore_recovers_queued_items(self):
        tid = self.thread()
        queue_db = self.home / "queue_1.sqlite"
        with closing(sqlite3.connect(queue_db)) as conn, conn:
            conn.execute("""CREATE TABLE queued_items (
                id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
            )""")
            conn.execute("""CREATE TABLE queued_thread_revisions (
                revision INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL UNIQUE
            )""")
            conn.execute("""CREATE TRIGGER queued_items_revision_after_insert
                AFTER INSERT ON queued_items BEGIN
                    INSERT INTO queued_thread_revisions (thread_id) VALUES (NEW.thread_id)
                    ON CONFLICT(thread_id) DO UPDATE SET revision =
                    (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions);
                END""")
            conn.execute("""CREATE TRIGGER queued_items_revision_after_delete
                AFTER DELETE ON queued_items BEGIN
                    INSERT INTO queued_thread_revisions (thread_id) VALUES (OLD.thread_id)
                    ON CONFLICT(thread_id) DO UPDATE SET revision =
                    (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions);
                END""")
            conn.execute("INSERT INTO queued_items VALUES ('queued-1', ?, 'pending', 0, 1, 1)", (tid,))
            initial_revision = conn.execute(
                "SELECT revision FROM queued_thread_revisions WHERE thread_id=?", (tid,)
            ).fetchone()[0]

        def delete_chat_and_queue(args, **kwargs):
            result = self.native_delete(args, **kwargs)
            with closing(sqlite3.connect(queue_db)) as conn, conn:
                conn.execute("DELETE FROM queued_items WHERE thread_id=?", (tid,))
            return result

        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.163.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=delete_chat_and_queue):
            result = execute(Store(self.home), Store(self.home).plan(30, now=NOW))

        with closing(sqlite3.connect(queue_db)) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM queued_items").fetchone()[0])
        restore(Store(self.home), result["backup"])
        with closing(sqlite3.connect(queue_db)) as conn:
            self.assertEqual((tid, "pending"), conn.execute(
                "SELECT thread_id, payload_json FROM queued_items WHERE id='queued-1'").fetchone())
            restored_revision = conn.execute(
                "SELECT revision FROM queued_thread_revisions WHERE thread_id=?", (tid,)
            ).fetchone()[0]
        self.assertGreater(restored_revision, initial_revision)

    def test_restore_with_multiple_name_history_entries_is_repeatable(self):
        tid = self.thread()
        index = self.home / "session_index.jsonl"
        entries = [
            json.dumps({"id": tid, "thread_name": "first name"}) + "\n",
            json.dumps({"id": tid, "thread_name": "second name"}) + "\n",
        ]
        index.write_text("".join(entries))
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        index.unlink()

        restore(Store(self.home), result["backup"])
        restore(Store(self.home), result["backup"])

        self.assertEqual(entries, index.read_text().splitlines(keepends=True))

    def test_no_backup_hashes_only_the_current_deletion_subtree(self):
        for _ in range(10):
            self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=self.native_delete), \
             patch("codex_kit.cleanup.digest", wraps=digest) as digest_mock:
            execute(Store(self.home), plan, backup=False)

        self.assertEqual(20, digest_mock.call_count)

    def test_changed_archive_state_aborts_before_delete(self):
        tid = self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE threads SET archived=0 WHERE id=?", (tid,))
        with self.assertRaises(StoreError):
            self.run_plan(plan)
        self.assertIn(tid, Store(self.home).read()[0])

    def test_new_active_descendant_aborts(self):
        parent = self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        child = self.thread(archived=False)
        self.edge(parent, child)
        with self.assertRaises(StoreError):
            self.run_plan(plan)
        self.assertEqual(2, len(Store(self.home).read()[0]))

    def test_native_failure_stops_and_records_recoverable_batch(self):
        from subprocess import CompletedProcess
        tid = self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", return_value=CompletedProcess([], 1, "", "blocked by fork history")):
            with self.assertRaises(StoreError) as caught:
                execute(Store(self.home), plan, backup_dir=self.home.parent / "backups")
        self.assertIn("blocked by fork history", str(caught.exception))
        manifest = next((self.home.parent / "backups").glob("*/manifest.json"))
        data = json.loads(manifest.read_text())
        self.assertEqual("failed", data["status"])
        self.assertIn(tid, Store(self.home).read()[0])

    def test_restore_rejects_tampered_backup_before_writes(self):
        self.thread()
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        batch = self.home.parent / "backups" / result["batch_id"]
        next((batch / "files").rglob("*.jsonl")).write_text("tampered")
        with self.assertRaises(StoreError):
            restore(Store(self.home), result["backup"])
        self.assertEqual({}, Store(self.home).read()[0])

    def test_restore_row_conflict_refuses_overwrite(self):
        tid = self.thread()
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        self.thread(thread_id=tid, title="replacement")
        with self.assertRaises(StoreError):
            restore(Store(self.home), result["backup"])
        self.assertEqual("replacement", Store(self.home).read()[0][tid].title)

    def test_process_success_without_actual_deletion_is_failure(self):
        from subprocess import CompletedProcess
        self.thread()
        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")):
            with self.assertRaises(StoreError):
                execute(Store(self.home), Store(self.home).plan(30, now=NOW), backup_dir=self.home.parent / "backups")

    def test_second_root_failure_retains_first_success_and_restores_both(self):
        from subprocess import CompletedProcess
        first = self.thread()
        second = self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        calls = []

        def delete_or_fail(args, **kwargs):
            calls.append(args[-1])
            if len(calls) == 1:
                return self.native_delete(args, **kwargs)
            return CompletedProcess(args, 1, "", "second failed")

        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=delete_or_fail):
            with self.assertRaises(StoreError):
                execute(Store(self.home), plan, backup_dir=self.home.parent / "backups")
        manifest = next((self.home.parent / "backups").glob("*/manifest.json"))
        self.assertEqual([calls[0]], json.loads(manifest.read_text())["deleted"])
        restore(Store(self.home), str(manifest.parent))
        self.assertEqual({first, second}, set(Store(self.home).read()[0]))

    def test_content_changed_after_backup_aborts(self):
        from codex_kit.cleanup import create_backup
        self.thread()
        plan = Store(self.home).plan(30, now=NOW)

        def backup_and_change(*args):
            batch, data = create_backup(*args)
            plan.threads[0].files[0].write_text("changed after backup")
            return batch, data

        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.create_backup", side_effect=backup_and_change), \
             patch("codex_kit.cleanup.subprocess.run") as runner:
            with self.assertRaises(StoreError):
                execute(Store(self.home), plan, backup_dir=self.home.parent / "backups")
        runner.assert_not_called()

    def test_separate_history_database_is_restored(self):
        tid = self.thread()
        history = self.home / "thread_history_1.sqlite"
        with closing(sqlite3.connect(history)) as conn, conn:
            conn.execute("CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, text TEXT, PRIMARY KEY(thread_id,item_id))")
            conn.execute("INSERT INTO thread_items VALUES (?, 'a', 'original')", (tid,))

        def delete_with_history(args, **kwargs):
            result = self.native_delete(args, **kwargs)
            with closing(sqlite3.connect(history)) as conn, conn:
                conn.execute("DELETE FROM thread_items WHERE thread_id=?", (tid,))
            return result

        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=delete_with_history):
            result = execute(Store(self.home), Store(self.home).plan(30, now=NOW), backup_dir=self.home.parent / "backups")
        restore(Store(self.home), result["backup"])
        with closing(sqlite3.connect(history)) as conn:
            self.assertEqual('original', conn.execute("SELECT text FROM thread_items").fetchone()[0])

    def test_restore_detects_index_name_conflict_before_writing(self):
        tid = self.thread()
        index = self.home / "session_index.jsonl"
        index.write_text(json.dumps({"id": tid, "thread_name": "original"}) + "\n")
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        index.write_text(json.dumps({"id": tid, "thread_name": "different"}) + "\n")
        with self.assertRaises(StoreError):
            restore(Store(self.home), result["backup"])
        self.assertEqual({}, Store(self.home).read()[0])

    def test_restore_interrupted_file_copy_can_be_retried(self):
        import shutil
        tid = self.thread()
        result = self.run_plan(Store(self.home).plan(30, now=NOW))

        def broken_copy(source, target):
            target.write(b"partial")
            raise OSError("disk full")

        with patch("codex_kit.cleanup.shutil.copyfileobj", side_effect=broken_copy):
            with self.assertRaises(OSError):
                restore(Store(self.home), result["backup"])
        restore(Store(self.home), result["backup"])
        self.assertIn(tid, Store(self.home).read()[0])

    def test_restore_interrupted_index_write_can_be_retried(self):
        tid = self.thread()
        index = (self.home / "session_index.jsonl").resolve()
        index.write_text(json.dumps({"id": tid, "thread_name": "example"}) + "\n")
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        unrelated = {"id": "unrelated", "thread_name": "unrelated"}
        index.write_text(json.dumps(unrelated) + "\n")

        original_named_temporary_file = tempfile.NamedTemporaryFile

        def interrupted_named_temporary_file(*args, **kwargs):
            handle = original_named_temporary_file(*args, **kwargs)
            if Path(kwargs["dir"]).resolve() == index.parent:
                class PartialWriter:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc_info):
                        return handle.__exit__(*exc_info)

                    def writelines(self, lines):
                        line = next(iter(lines))
                        handle.write(line[:max(1, len(line) // 2)])
                        handle.flush()
                        raise OSError("disk full")

                    def __getattr__(self, name):
                        return getattr(handle, name)

                return PartialWriter()
            return handle

        with patch("codex_kit.cleanup.tempfile.NamedTemporaryFile", side_effect=interrupted_named_temporary_file):
            with self.assertRaises(OSError):
                restore(Store(self.home), result["backup"])

        restore(Store(self.home), result["backup"])
        records = [json.loads(line) for line in index.read_text().splitlines()]
        self.assertEqual([unrelated, {"id": tid, "thread_name": "example"}], records)

    def test_restore_checks_existing_thread_rows_before_restoring_any_file(self):
        tid = self.thread()
        result = self.run_plan(Store(self.home).plan(30, now=NOW))
        self.thread(thread_id=tid, title="replacement")
        paths = Store(self.home).read()[0][tid].files
        for path in paths:
            path.unlink()
        with self.assertRaises(StoreError):
            restore(Store(self.home), result["backup"])
        self.assertFalse(any(path.exists() for path in paths))

    def test_restore_recovers_per_thread_memory_job(self):
        tid = self.thread()
        memory = self.home / "memories_1.sqlite"
        with closing(sqlite3.connect(memory)) as conn, conn:
            conn.execute("CREATE TABLE jobs (kind TEXT, job_key TEXT, status TEXT, PRIMARY KEY(kind,job_key))")
            conn.execute("INSERT INTO jobs VALUES ('memory_stage1', ?, 'done')", (tid,))
            conn.execute("INSERT INTO jobs VALUES ('global', 'all', 'ready')")

        def delete_with_job(args, **kwargs):
            result = self.native_delete(args, **kwargs)
            with closing(sqlite3.connect(memory)) as conn, conn:
                conn.execute("DELETE FROM jobs WHERE job_key=?", (tid,))
            return result

        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=delete_with_job):
            result = execute(Store(self.home), Store(self.home).plan(30, now=NOW), backup_dir=self.home.parent / "backups")
        restore(Store(self.home), result["backup"])
        with closing(sqlite3.connect(memory)) as conn:
            self.assertEqual(('done',), conn.execute("SELECT status FROM jobs WHERE job_key=?", (tid,)).fetchone())
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_no_backup_deletes_without_copying_chat_data(self):
        tid = self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.create_backup") as backup, \
             patch("codex_kit.cleanup.subprocess.run", side_effect=self.native_delete):
            result = execute(Store(self.home), plan, backup=False)
        backup.assert_not_called()
        self.assertEqual([tid], result["deleted"])
        self.assertIsNone(result["backup"])
        self.assertIsNone(result["batch_id"])
        self.assertEqual({}, Store(self.home).read()[0])
        self.assertEqual(["operation.lock"], sorted(p.name for p in (self.home / "codex-kit").iterdir()))

    def test_no_backup_failure_reports_completed_deletions(self):
        from subprocess import CompletedProcess
        self.thread()
        self.thread()
        plan = Store(self.home).plan(30, now=NOW)
        calls = []

        def delete_or_fail(args, **kwargs):
            calls.append(args[-1])
            if len(calls) == 1:
                return self.native_delete(args, **kwargs)
            return CompletedProcess(args, 1, "", "second failed")

        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", side_effect=delete_or_fail):
            with self.assertRaises(StoreError) as caught:
                execute(Store(self.home), plan, backup=False)
        self.assertIn(calls[0], str(caught.exception))
        self.assertIn("second failed", str(caught.exception))
        self.assertIn("No backup", str(caught.exception))
        self.assertEqual(1, len(Store(self.home).read()[0]))

    def test_no_backup_keeps_success_verification(self):
        from subprocess import CompletedProcess
        self.thread()
        with patch("codex_kit.cleanup.check_codex", return_value="codex-cli 0.146.0"), \
             patch("codex_kit.cleanup.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")):
            with self.assertRaises(StoreError):
                execute(Store(self.home), Store(self.home).plan(30, now=NOW), backup=False)
