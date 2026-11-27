"""Opt-in integration test against a real Codex executable, using synthetic chats."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import uuid

from codex_kit.cleanup import execute, restore
from codex_kit.storage import Store


@unittest.skipUnless(os.environ.get("CODEX_KIT_NATIVE_TEST") == "1", "set CODEX_KIT_NATIVE_TEST=1 for real Codex integration")
class NativeIntegrationTests(unittest.TestCase):
    def test_native_parent_child_delete_and_restore(self):
        binary = shutil.which("codex")
        self.assertIsNotNone(binary)
        with tempfile.TemporaryDirectory(prefix="codex-kit-native-") as temporary:
            home = Path(temporary).resolve()
            env = dict(os.environ, CODEX_HOME=str(home))
            # A nonexistent UUID initializes the official schema and deletes no thread.
            init = subprocess.run([binary, "delete", "--force", str(uuid.uuid4())],
                                  env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, init.returncode)
            self.assertTrue((home / "state_5.sqlite").exists(), init.stderr)
            ids = [str(uuid.uuid4()), str(uuid.uuid4())]
            for tid in ids:
                path = home / "archived_sessions" / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({
                    "timestamp": "2026-01-01T00:00:00.000Z", "type": "session_meta",
                    "payload": {"id": tid, "timestamp": "2026-01-01T00:00:00.000Z", "cwd": str(home),
                                "originator": "codex_cli_rs", "cli_version": "0.146.0", "source": "cli", "model_provider": "openai"},
                }) + "\n")
                with closing(sqlite3.connect(home / "state_5.sqlite")) as conn, conn:
                    conn.execute("""INSERT INTO threads
                        (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,
                         sandbox_policy,approval_mode,archived,archived_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (tid, str(path), 1700000000, 1700000000, "cli", "openai", str(home),
                         "synthetic integration chat", '"read-only"', "never", 1, 1700000000))
            with closing(sqlite3.connect(home / "state_5.sqlite")) as conn, conn:
                conn.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'closed')", ids)
            store = Store(home)
            plan = store.plan(30)
            self.assertEqual([ids[0]], plan.roots)
            result = execute(store, plan, codex=binary)
            self.assertEqual(set(ids), set(result["deleted"]))
            self.assertEqual({}, store.read()[0])
            restore(store, result["backup"])
            restored, edges = store.read()
            self.assertEqual(set(ids), set(restored))
            self.assertEqual({ids[1]}, edges[ids[0]])
            self.assertTrue(all(t.files for t in restored.values()))
            backups = sorted((home / "codex-kit" / "backups").iterdir())
            direct = execute(store, store.plan(30), codex=binary, backup=False)
            self.assertEqual(set(ids), set(direct["deleted"]))
            self.assertIsNone(direct["backup"])
            self.assertEqual({}, store.read()[0])
            self.assertEqual(backups, sorted((home / "codex-kit" / "backups").iterdir()))
