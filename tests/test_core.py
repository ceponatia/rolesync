from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rolesync.cli import init_project
from rolesync.core import RoleSyncError, MANIFEST, render, sync


class CoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project with ünicode"
        init_project(self.root, "minimal", "both")

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_traversal_cannot_delete_unrelated_file(self):
        victim = self.root / "README.md"
        victim.write_text("keep me\n", encoding="utf-8")
        manifest_path = self.root / MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][".claude/agents/../../README.md"] = hashlib.sha256(victim.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(RoleSyncError, "Unsafe path"):
            sync(self.root)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")

    def test_symlinked_managed_directory_cannot_escape_project(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        agents_dir = self.root / ".claude/agents"
        shutil.rmtree(agents_dir)
        try:
            agents_dir.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation not permitted: {exc}")
        source = self.root / ".agents/roles/coder.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
        with self.assertRaisesRegex(RoleSyncError, "symlink|escapes"):
            sync(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_binary_skill_resource_is_mirrored_byte_for_byte(self):
        data = bytes(range(256))
        asset = self.root / ".agents/skills/rolesync-implement/fixture.bin"
        asset.write_bytes(data)
        sync(self.root)
        mirror = self.root / ".claude/skills/rolesync-implement/fixture.bin"
        self.assertEqual(mirror.read_bytes(), data)

    def test_likely_secret_in_skill_resources_is_rejected(self):
        secret = self.root / ".agents/skills/rolesync-implement/.env"
        secret.write_text("TOKEN=secret\n", encoding="utf-8")
        with self.assertRaisesRegex(RoleSyncError, "likely secret"):
            render(self.root)

    def test_platform_selection_limits_owned_outputs(self):
        config = self.root / ".agents/rolesync.json"
        config.write_text(json.dumps({"schema_version": 1, "platforms": ["codex"]}), encoding="utf-8")
        sync(self.root)
        self.assertTrue((self.root / ".codex/agents/rolesync-coder.toml").is_file())
        self.assertFalse((self.root / ".claude/agents/rolesync-coder.md").exists())
        self.assertFalse((self.root / ".claude/skills/rolesync-implement/SKILL.md").exists())

    def test_legacy_loom_config_remains_supported(self):
        config = self.root / ".agents/rolesync.json"
        legacy = self.root / ".agents/loom.json"
        config.rename(legacy)
        legacy.write_text(json.dumps({"schema_version": 1, "platforms": ["codex"]}), encoding="utf-8")
        sync(self.root)
        self.assertTrue((self.root / ".codex/agents/rolesync-coder.toml").is_file())
        self.assertFalse((self.root / ".claude/agents/rolesync-coder.md").exists())

    def test_crlf_skill_mirror_remains_owned_when_platform_is_removed(self):
        skill = self.root / ".claude/skills/rolesync-implement/SKILL.md"
        data = skill.read_bytes()
        if b"\r\n" not in data:
            skill.write_bytes(data.replace(b"\n", b"\r\n"))
        config = self.root / ".agents/rolesync.json"
        config.write_text(json.dumps({"schema_version": 1, "platforms": ["codex"]}), encoding="utf-8")
        sync(self.root)
        self.assertFalse(skill.exists())

    def test_disabled_role_is_not_rendered(self):
        catalog_path = self.root / ".agents/catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        reviewer = next(r for r in catalog["roles"] if r["id"] == "reviewer")
        reviewer["enabled"] = False
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        sync(self.root)
        self.assertFalse((self.root / ".claude/agents/rolesync-reviewer.md").exists())
        self.assertFalse((self.root / ".codex/agents/rolesync-reviewer.toml").exists())

    def test_manifest_is_stable_when_canonical_skill_text_uses_crlf(self):
        source = self.root / ".agents/skills/rolesync-implement/SKILL.md"
        mirror = self.root / ".claude/skills/rolesync-implement/SKILL.md"
        source_data = source.read_bytes()
        mirror_data = mirror.read_bytes()
        if b"\r\n" not in source_data:
            source.write_bytes(source_data.replace(b"\n", b"\r\n"))
        if b"\r\n" not in mirror_data:
            mirror.write_bytes(mirror_data.replace(b"\n", b"\r\n"))
        self.assertEqual(sync(self.root, check=True), [])

    def test_check_ignores_checkout_only_crlf_for_native_outputs_and_manifest(self):
        for rel in [
            ".claude/agents/rolesync-coder.md",
            ".codex/agents/rolesync-coder.toml",
            ".agents/generated-manifest.json",
        ]:
            path = self.root / rel
            data = path.read_bytes()
            if b"\r\n" not in data:
                path.write_bytes(data.replace(b"\n", b"\r\n"))
        self.assertEqual(sync(self.root, check=True), [])

    def test_crlf_generated_text_remains_owned(self):
        generated = self.root / ".claude/agents/rolesync-coder.md"
        generated.write_bytes(generated.read_bytes().replace(b"\n", b"\r\n"))
        source = self.root / ".agents/roles/coder.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nA legitimate source update.\n", encoding="utf-8")
        sync(self.root)
        self.assertEqual(sync(self.root, check=True), [])

    def test_crlf_touch_is_ignored_for_non_markdown_skill_resources(self):
        resource = self.root / ".agents/skills/rolesync-implement/notes.json"
        resource.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
        sync(self.root)
        mirror = self.root / ".claude/skills/rolesync-implement/notes.json"
        data = mirror.read_bytes()
        if b"\r\n" not in data:
            mirror.write_bytes(data.replace(b"\n", b"\r\n"))
        self.assertEqual(sync(self.root, check=True), [])

    def test_check_reports_catalog_error_alongside_incomplete_transaction(self):
        catalog_path = self.root / ".agents/catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["roles"][0]["skill"] = "does-not-exist"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        (self.root / ".agents/.rolesync-txn-test").mkdir(parents=True)
        with self.assertRaisesRegex(RoleSyncError, "Missing skill"):
            sync(self.root, check=True)

    def test_invalid_generated_toml_is_reported_as_rolesync_error(self):
        with mock.patch("rolesync.core.tomllib.loads", side_effect=tomllib.TOMLDecodeError("bad toml")):
            with self.assertRaisesRegex(RoleSyncError, "invalid"):
                render(self.root)

    def test_transaction_backup_path_traversal_is_rejected(self):
        secret = Path(self.temp.name) / "secret.txt"
        secret.write_text("do not leak me\n", encoding="utf-8")
        target = self.root / ".claude/agents/rolesync-coder.md"
        original = target.read_bytes()
        txn = self.root / ".agents/.rolesync-txn-evil"
        (txn / "backups").mkdir(parents=True)
        (txn / "journal.json").write_text(json.dumps({
            "state": "applying",
            "operations": [{"rel": ".claude/agents/rolesync-coder.md", "backup": str(secret), "created": False, "stage": None}],
        }), encoding="utf-8")
        with self.assertRaisesRegex(RoleSyncError, "Invalid transaction backup reference"):
            sync(self.root)
        self.assertEqual(target.read_bytes(), original)

    def _write_executable_helper(self) -> tuple[Path, Path]:
        if os.name == "nt":
            self.skipTest("executable bits are not tracked on Windows")
        source = self.root / ".agents/skills/rolesync-implement/helper.sh"
        source.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        source.chmod(source.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return source, self.root / ".claude/skills/rolesync-implement/helper.sh"

    def test_executable_skill_resource_is_mirrored_executable(self):
        source, mirror = self._write_executable_helper()
        sync(self.root)
        self.assertTrue(mirror.stat().st_mode & stat.S_IXUSR)
        self.assertTrue(mirror.stat().st_mode & stat.S_IXGRP)
        self.assertEqual(mirror.read_bytes(), source.read_bytes())
        self.assertEqual(sync(self.root, check=True), [])
        skill_md = self.root / ".claude/skills/rolesync-implement/SKILL.md"
        self.assertFalse(skill_md.stat().st_mode & stat.S_IXUSR)

    def test_dropped_executable_bit_is_reported_and_repaired(self):
        _, mirror = self._write_executable_helper()
        sync(self.root)
        mirror.chmod(mirror.stat().st_mode & ~0o111)
        self.assertEqual(sync(self.root, check=True), ["mode:.claude/skills/rolesync-implement/helper.sh"])
        sync(self.root)
        self.assertTrue(mirror.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(sync(self.root, check=True), [])

    def test_spurious_executable_bit_on_mirror_is_reported_and_repaired(self):
        if os.name == "nt":
            self.skipTest("executable bits are not tracked on Windows")
        mirror = self.root / ".claude/skills/rolesync-implement/SKILL.md"
        mirror.chmod(mirror.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(sync(self.root, check=True), ["mode:.claude/skills/rolesync-implement/SKILL.md"])
        sync(self.root)
        self.assertFalse(mirror.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(sync(self.root, check=True), [])

    def test_executable_bit_survives_transaction_rollback(self):
        source, mirror = self._write_executable_helper()
        sync(self.root)
        source.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        with mock.patch("rolesync.core.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sync(self.root)
        self.assertEqual(mirror.read_text(encoding="utf-8"), "#!/bin/sh\necho ok\n")
        self.assertTrue(mirror.stat().st_mode & stat.S_IXUSR)

    def test_generated_files_name_the_rolesync_command(self):
        for rel in (".claude/agents/rolesync-coder.md", ".codex/agents/rolesync-coder.toml"):
            text = (self.root / rel).read_text(encoding="utf-8")
            self.assertIn("Generated by rolesync sync; edit canonical .agents sources.", text)
            self.assertNotIn("scripts/sync_agents.py", text)

    def test_project_generated_with_legacy_marker_resyncs_cleanly(self):
        legacy = "Generated by scripts/sync_agents.py; edit canonical .agents sources."
        with mock.patch("rolesync.core.MARKER", legacy):
            sync(self.root)
        claude = self.root / ".claude/agents/rolesync-coder.md"
        self.assertIn(legacy, claude.read_text(encoding="utf-8"))
        drift = sync(self.root, check=True)
        self.assertIn(".claude/agents/rolesync-coder.md", drift)
        self.assertIn(".codex/agents/rolesync-coder.toml", drift)
        sync(self.root)
        self.assertNotIn(legacy, claude.read_text(encoding="utf-8"))
        self.assertEqual(sync(self.root, check=True), [])

    def test_incomplete_transaction_is_recovered_before_sync(self):
        target = self.root / ".claude/agents/rolesync-coder.md"
        original = target.read_bytes()
        txn = self.root / ".agents/.rolesync-txn-test"
        backups = txn / "backups"
        backups.mkdir(parents=True)
        (backups / "0.bak").write_bytes(original)
        (txn / "journal.json").write_text(json.dumps({
            "state": "applying",
            "operations": [{"rel": ".claude/agents/rolesync-coder.md", "backup": "0.bak", "created": False, "stage": "0.new"}],
        }), encoding="utf-8")
        target.write_text("partial write", encoding="utf-8")
        sync(self.root)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(txn.exists())


if __name__ == "__main__":
    unittest.main()
