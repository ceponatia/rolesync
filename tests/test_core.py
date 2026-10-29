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
from rolesync import core
from rolesync.core import RoleSyncError, MANIFEST, render, sync


class CoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / "project with ünicode").resolve()
        home = Path(self.temp.name) / "home"
        home.mkdir()
        self.home_patch = mock.patch("rolesync.core.Path.home", return_value=home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
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
        txn = core._transaction_home(self.root, create=True) / ".rolesync-txn-evil"
        txn.mkdir(mode=0o700)
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
        txn = core._transaction_home(self.root, create=True) / ".rolesync-txn-test"
        txn.mkdir(mode=0o700)
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

    def _recovery_fixture(self, *, trusted=True, rel=".claude/agents/rolesync-coder.md"):
        parent = core._transaction_home(self.root, create=True) if trusted else self.root / ".agents"
        txn = parent / ".rolesync-txn-fixture"
        txn.mkdir(mode=0o700)
        (txn / "backups").mkdir()
        target = self.root / rel
        (txn / "backups/0.bak").write_bytes(target.read_bytes())
        (txn / "journal.json").write_text(json.dumps({
            "state": "applying",
            "operations": [{"rel": rel, "backup": "0.bak", "created": False, "stage": "0.new"}],
        }), encoding="utf-8")
        return txn, target

    def _symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation not permitted: {exc}")

    def test_in_project_transaction_is_not_replayed(self):
        txn, target = self._recovery_fixture(trusted=False)
        original = target.read_bytes()
        (txn / "backups/0.bak").write_bytes(b"untrusted recovery data")
        with self.assertRaisesRegex(RoleSyncError, "untrusted in-project"):
            sync(self.root)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(txn.exists())
        self.assertIn("incomplete-transaction", sync(self.root, check=True))

    def test_repository_backup_directory_link_cannot_read_host_file(self):
        txn, target = self._recovery_fixture(trusted=False)
        original = target.read_bytes()
        outside = Path(self.temp.name) / "outside-backups"
        outside.mkdir()
        secret = outside / "0.bak"
        secret.write_bytes(b"private host data")
        shutil.rmtree(txn / "backups")
        self._symlink(txn / "backups", outside, directory=True)
        with self.assertRaises(RoleSyncError):
            sync(self.root)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(secret.read_bytes(), b"private host data")

    def test_repository_restore_link_cannot_overwrite_host_file(self):
        _, target = self._recovery_fixture(trusted=False)
        victim = Path(self.temp.name) / "host-file"
        victim.write_bytes(b"keep host file")
        self._symlink(target.with_name(target.name + ".rolesync-restore"), victim)
        with self.assertRaises(RoleSyncError):
            sync(self.root)
        self.assertEqual(victim.read_bytes(), b"keep host file")

    def test_trusted_recovery_ignores_old_predictable_temporary_leaf(self):
        txn, target = self._recovery_fixture()
        original = target.read_bytes()
        target.write_bytes(b"interrupted output")
        victim = Path(self.temp.name) / "host-file"
        victim.write_bytes(b"keep host file")
        old_temp = target.with_name(target.name + ".rolesync-restore")
        self._symlink(old_temp, victim)
        sync(self.root)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(victim.read_bytes(), b"keep host file")
        self.assertTrue(old_temp.is_symlink())
        self.assertFalse(txn.exists())

    def test_recovery_rejects_linked_components_before_writing(self):
        for component in ("transaction", "journal", "backups", "backup"):
            with self.subTest(component=component):
                txn, target = self._recovery_fixture()
                original = target.read_bytes()
                path = {"transaction": txn, "journal": txn / "journal.json", "backups": txn / "backups", "backup": txn / "backups/0.bak"}[component]
                moved = Path(self.temp.name) / ("moved-" + component)
                path.rename(moved)
                self._symlink(path, moved, directory=component in {"transaction", "backups"})
                with self.assertRaises(RoleSyncError):
                    sync(self.root)
                self.assertEqual(target.read_bytes(), original)
                path.unlink()
                moved.rename(path)
                shutil.rmtree(txn)

    def test_hardlinked_backup_is_rejected(self):
        txn, target = self._recovery_fixture()
        original = target.read_bytes()
        backup = txn / "backups/0.bak"
        backup.unlink()
        try:
            os.link(target, backup)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(RoleSyncError, "unlinked file"):
            sync(self.root)
        self.assertEqual(target.read_bytes(), original)

    def test_recovery_validates_all_operations_before_mutating(self):
        txn, target = self._recovery_fixture()
        target.write_bytes(b"interrupted output")
        journal = json.loads((txn / "journal.json").read_text())
        journal["operations"].insert(0, {"rel": ".claude/agents/missing.md", "backup": "missing.bak", "created": False})
        (txn / "journal.json").write_text(json.dumps(journal))
        with self.assertRaises(FileNotFoundError):
            sync(self.root)
        self.assertEqual(target.read_bytes(), b"interrupted output")

    def test_recovery_removes_only_outputs_created_by_interrupted_sync(self):
        txn, target = self._recovery_fixture()
        created = self.root / ".claude/agents/created.md"
        created.write_bytes(b"partial new output")
        journal = json.loads((txn / "journal.json").read_text())
        journal["operations"].append({"rel": ".claude/agents/created.md", "backup": None, "created": True})
        (txn / "journal.json").write_text(json.dumps(journal))
        sync(self.root)
        self.assertFalse(created.exists())
        self.assertTrue(target.exists())

    def test_transaction_state_is_outside_checkout_and_private(self):
        home = core._transaction_home(self.root, create=True)
        self.assertFalse(home.is_relative_to(self.root))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            home.chmod(0o755)
            with self.assertRaisesRegex(RoleSyncError, "private"):
                sync(self.root)

    def test_symlinked_state_root_is_rejected(self):
        state = core._transaction_home(self.root, create=True).parent
        moved = state.with_name("moved-state")
        state.rename(moved)
        self._symlink(state, moved, directory=True)
        with self.assertRaisesRegex(RoleSyncError, "normal directory"):
            sync(self.root)

    def test_failed_apply_is_recovered_on_next_sync(self):
        source = self.root / ".agents/roles/coder.md"
        source.write_text(source.read_text() + "\nChanged instructions.\n")
        replace = os.replace
        calls = 0

        def fail_after_first_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("simulated interruption")
            return replace(*args, **kwargs)

        with mock.patch("rolesync.core.os.replace", side_effect=fail_after_first_write):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                sync(self.root)
        self.assertTrue(core._transaction_dirs(self.root))
        sync(self.root)
        self.assertEqual(sync(self.root, check=True), [])
        self.assertEqual(core._transaction_dirs(self.root), [])

    def test_exclusive_restore_creation_cannot_follow_existing_link(self):
        victim = Path(self.temp.name) / "host-file"
        victim.write_bytes(b"keep host file")
        target = self.root / ".claude/agents/rolesync-coder.md"
        original = target.read_bytes()
        temp = target.parent / ".rolesync-write-collision"
        self._symlink(temp, victim)
        with mock.patch("rolesync.core.uuid.uuid4", return_value=mock.Mock(hex="collision")):
            with self.assertRaises(FileExistsError):
                core._atomic_transaction_write(self.root, target, b"new bytes", 0o644)
        self.assertEqual(victim.read_bytes(), b"keep host file")
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(temp.is_symlink())

    def test_restore_pins_destination_directory_during_replacement(self):
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("directory descriptors unavailable")
        target = self.root / ".claude/agents/rolesync-coder.md"
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        moved = target.parent.with_name("moved-agents")
        replace = os.replace

        def replace_after_swap(*args, **kwargs):
            target.parent.rename(moved)
            self._symlink(target.parent, outside, directory=True)
            return replace(*args, **kwargs)

        with mock.patch("rolesync.core.os.replace", side_effect=replace_after_swap):
            core._atomic_transaction_write(self.root, target, b"restored", 0o644)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((moved / target.name).read_bytes(), b"restored")
        self.assertFalse(list(moved.glob(".rolesync-write-*")))

    def test_check_does_not_create_private_state(self):
        home = core._transaction_home(self.root).parent
        shutil.rmtree(home)
        self.assertEqual(sync(self.root, check=True), [])
        self.assertFalse(home.exists())

    def test_recovery_restores_manifest_as_well_as_outputs(self):
        txn, target = self._recovery_fixture(rel=MANIFEST)
        original = target.read_bytes()
        target.write_bytes(b"interrupted manifest")
        sync(self.root)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(txn.exists())


if __name__ == "__main__":
    unittest.main()
