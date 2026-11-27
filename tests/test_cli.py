import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_storage import StoreFixture


class CLITests(StoreFixture):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "codex_kit", "--home", str(self.home), *args],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def test_help_entrypoint(self):
        result = self.cli("--help")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("prune", result.stdout)

    def test_preview_is_read_only(self):
        tid = self.thread(age=400)
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        result = self.cli("prune", "--archived", "--days", "30", "--dry-run", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([tid], [t["id"] for t in data["threads"]])
        after = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_noninteractive_delete_requires_yes(self):
        self.thread(age=400)
        result = self.cli("prune", "--days", "30")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--yes", result.stderr)
        self.assertFalse((self.home / "codex-kit").exists())

    def test_list_defaults_to_archived_and_stats_counts_both(self):
        archived = self.thread()
        self.thread(archived=False)
        result = self.cli("list", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([archived], [t["id"] for t in json.loads(result.stdout)["threads"]])
        result = self.cli("stats", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(1, data["archived"])
        self.assertEqual(1, data["active"])
        self.assertGreater(data["rollout_bytes"], 0)

    def test_bad_days_is_a_user_error(self):
        result = self.cli("prune", "--days", "0", "--dry-run")
        self.assertEqual(2, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_home_option_after_subcommand(self):
        result = subprocess.run([sys.executable, "-m", "codex_kit", "list", "--home", str(self.home), "--json"],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_json_errors_are_machine_readable(self):
        result = self.cli("prune", "--days", "-1", "--dry-run", "--json")
        self.assertEqual(2, result.returncode)
        self.assertIn("error", json.loads(result.stderr))

    def test_no_backup_preview_is_read_only_and_describes_mode(self):
        self.thread(age=400)
        result = self.cli("prune", "--days", "30", "--no-backup", "--dry-run", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(json.loads(result.stdout)["backup_enabled"])
        self.assertFalse((self.home / "codex-kit").exists())

    def test_no_backup_still_requires_confirmation(self):
        self.thread(age=400)
        result = self.cli("prune", "--days", "30", "--no-backup")
        self.assertEqual(2, result.returncode)
        self.assertIn("--yes", result.stderr)
        self.assertIn("cannot be restored", result.stdout)
        self.assertFalse((self.home / "codex-kit").exists())

    def test_no_backup_conflicts_with_backup_directory(self):
        result = self.cli("prune", "--days", "30", "--no-backup", "--backup-dir", str(self.home / "backups"))
        self.assertEqual(2, result.returncode)
        self.assertIn("not allowed", result.stderr)
