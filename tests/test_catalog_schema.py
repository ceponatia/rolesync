"""Catalog schema tests for the optional fields added after the vesper adoption."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rolesync.cli import init_project
from rolesync.core import RoleSyncError, render, sync


def frontmatter(text: str) -> dict:
    """Parse the generator's restricted JSON-value YAML, not arbitrary YAML."""
    header = text.split("---\n", 2)[1]
    return {k: json.loads(v) for k, v in (line.split(": ", 1) for line in header.splitlines() if line.strip())}


class CatalogSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        init_project(self.root, "minimal", "both")
        self.catalog_path = self.root / ".agents/catalog.json"
        self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.coder = next(r for r in self.catalog["roles"] if r["id"] == "coder")

    def tearDown(self):
        self.temp.cleanup()

    def save(self) -> None:
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")

    def claude(self, name: str = "rolesync-coder") -> str:
        return (self.root / f".claude/agents/{name}.md").read_text(encoding="utf-8")

    def codex(self, name: str = "rolesync-coder") -> dict:
        return tomllib.loads((self.root / f".codex/agents/{name}.toml").read_text(encoding="utf-8"))

    def assert_rejected(self, pattern: str) -> None:
        self.save()
        with self.assertRaisesRegex(RoleSyncError, pattern):
            render(self.root)

    # --- optional native keys -------------------------------------------------

    def test_omitted_or_null_native_keys_render_no_line(self):
        del self.coder["claude"]["tools"]
        self.coder["claude"]["permissionMode"] = None
        del self.coder["claude"]["maxTurns"]
        del self.coder["codex"]["sandbox_mode"]
        self.save()
        sync(self.root)
        text = self.claude()
        fm = frontmatter(text)
        for key in ("tools", "permissionMode", "maxTurns"):
            self.assertNotIn(key, fm)
            self.assertNotIn(f"\n{key}:", text)
        self.assertEqual(fm["model"], "sonnet")
        self.assertEqual(fm["skills"], ["rolesync-implement"])
        cx = self.codex()
        self.assertNotIn("sandbox_mode", cx)
        self.assertEqual(cx["model"], "gpt-5.6-terra")
        other = frontmatter(self.claude("rolesync-reviewer"))
        self.assertEqual(other["permissionMode"], "default")
        self.assertEqual(self.codex("rolesync-reviewer")["sandbox_mode"], "read-only")
        self.assertEqual(sync(self.root, check=True), [])

    def test_present_native_keys_are_still_validated(self):
        cases = [
            ("claude", "tools", "Read", r"claude\.tools"),
            ("claude", "tools", [""], r"claude\.tools"),
            ("claude", "permissionMode", 3, r"claude\.permissionMode"),
            ("claude", "maxTurns", 0, r"claude\.maxTurns"),
            ("claude", "maxTurns", True, r"claude\.maxTurns"),
            ("codex", "sandbox_mode", "", r"codex\.sandbox_mode"),
            ("codex", "model", None, r"codex requires"),
            ("claude", "model", None, r"claude\.model"),
        ]
        for section, key, bad, pattern in cases:
            with self.subTest(section=section, key=key, bad=bad):
                original = self.coder[section][key]
                self.coder[section][key] = bad
                self.assert_rejected(pattern)
                self.coder[section][key] = original

    def test_star_tools_list_renders_verbatim(self):
        self.coder["claude"]["tools"] = ["*"]
        self.save()
        sync(self.root)
        self.assertEqual(frontmatter(self.claude())["tools"], ["*"])

    # --- skills ---------------------------------------------------------------

    def test_role_without_skill_gets_no_preload_and_no_workflow_line(self):
        for variant in ("absent", "null"):
            with self.subTest(variant=variant):
                if variant == "absent":
                    self.coder.pop("skill", None)
                else:
                    self.coder["skill"] = None
                self.save()
                sync(self.root)
                text = self.claude()
                self.assertNotIn("skills", frontmatter(text))
                self.assertNotIn("Primary workflow", text)
                self.assertNotIn("Primary workflow", self.codex()["developer_instructions"])
                self.assertIn("Primary workflow", self.claude("rolesync-reviewer"))

    def test_skills_list_is_merged_with_skill_and_validated(self):
        self.coder["skills"] = ["rolesync-review", "rolesync-implement"]
        self.save()
        sync(self.root)
        text = self.claude()
        self.assertEqual(frontmatter(text)["skills"], ["rolesync-implement", "rolesync-review"])
        line = "Workflow skills: `.agents/skills/rolesync-implement/SKILL.md`, `.agents/skills/rolesync-review/SKILL.md`."
        self.assertIn(line, text)
        self.assertIn(line, self.codex()["developer_instructions"])
        self.assertNotIn("Primary workflow", text)
        self.coder["skills"] = ["does-not-exist"]
        self.assert_rejected("Missing skill: does-not-exist")
        self.coder["skills"] = "rolesync-review"
        self.assert_rejected(r"\.skills must be a list")
        self.coder["skills"] = ["Not Kebab"]
        self.assert_rejected(r"\.skills must be a list")

    def test_skills_list_alone_replaces_skill(self):
        del self.coder["skill"]
        self.coder["skills"] = ["rolesync-review"]
        self.save()
        sync(self.root)
        text = self.claude()
        self.assertEqual(frontmatter(text)["skills"], ["rolesync-review"])
        self.assertIn("Primary workflow: `.agents/skills/rolesync-review/SKILL.md`.", text)

    def test_preload_skills_false_keeps_body_reference_only(self):
        self.coder["claude"]["preloadSkills"] = False
        self.save()
        sync(self.root)
        text = self.claude()
        self.assertNotIn("skills", frontmatter(text))
        self.assertIn("Primary workflow: `.agents/skills/rolesync-implement/SKILL.md`.", text)
        self.coder["claude"]["preloadSkills"] = "no"
        self.assert_rejected(r"claude\.preloadSkills must be a boolean")

    # --- Claude passthrough ---------------------------------------------------

    def test_allowlisted_claude_frontmatter_passes_through_verbatim(self):
        extra = {
            "disallowedTools": ["Bash", "Write"],
            "color": "blue",
            "mcpServers": ["github", {"name": "local", "command": "srv"}],
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
            "memory": "project",
            "background": True,
            "initialPrompt": "Start with the brief.",
            "omitClaudeMd": False,
            "unknownKey": "ignored",
        }
        self.coder["claude"].update(extra)
        self.save()
        sync(self.root)
        text = self.claude()
        fm = frontmatter(text)
        for key, value in extra.items():
            if key == "unknownKey":
                self.assertNotIn(key, fm)
            else:
                self.assertEqual(fm[key], value)
        keys = list(fm)
        self.assertLess(keys.index("isolation"), keys.index("disallowedTools"))
        self.assertLess(keys.index("disallowedTools"), keys.index("omitClaudeMd"))
        self.assertNotIn("disallowedTools", self.codex())
        self.assertEqual(sync(self.root, check=True), [])

    def test_allowlisted_claude_frontmatter_is_type_checked(self):
        cases = [
            ("disallowedTools", "Bash"),
            ("disallowedTools", [1]),
            ("color", ""),
            ("mcpServers", "github"),
            ("hooks", []),
            ("memory", 1),
            ("background", "yes"),
            ("initialPrompt", 2),
            ("omitClaudeMd", 0),
        ]
        for key, bad in cases:
            with self.subTest(key=key, bad=bad):
                self.coder["claude"][key] = bad
                self.assert_rejected(rf"claude\.{key} must be")
                del self.coder["claude"][key]

    # --- per-platform controls -------------------------------------------------

    def test_role_platforms_limit_output_and_prune_the_other_platform(self):
        codex_file = self.root / ".codex/agents/rolesync-coder.toml"
        claude_file = self.root / ".claude/agents/rolesync-coder.md"
        self.assertTrue(codex_file.is_file())
        self.coder["platforms"] = ["claude"]
        self.save()
        self.assertIn("stale:.codex/agents/rolesync-coder.toml", sync(self.root, check=True))
        sync(self.root)
        self.assertTrue(claude_file.is_file())
        self.assertFalse(codex_file.exists())
        self.assertTrue((self.root / ".codex/agents/rolesync-reviewer.toml").is_file())
        self.assertEqual(sync(self.root, check=True), [])
        self.coder["platforms"] = ["codex"]
        self.save()
        sync(self.root)
        self.assertTrue(codex_file.is_file())
        self.assertFalse(claude_file.exists())
        self.assertEqual(sync(self.root, check=True), [])

    def test_role_platforms_intersect_with_project_platforms(self):
        (self.root / ".agents/rolesync.json").write_text(
            json.dumps({"schema_version": 1, "platforms": ["codex"]}), encoding="utf-8"
        )
        self.coder["platforms"] = ["claude"]
        self.save()
        outputs = render(self.root)
        self.assertNotIn(".codex/agents/rolesync-coder.toml", outputs)
        self.assertNotIn(".claude/agents/rolesync-coder.md", outputs)
        self.assertIn(".codex/agents/rolesync-reviewer.toml", outputs)

    def test_invalid_role_platforms_are_rejected(self):
        for bad in ([], ["gemini"], ["claude", "claude"], "claude"):
            with self.subTest(bad=bad):
                self.coder["platforms"] = bad
                self.assert_rejected(r"\.platforms must be a non-empty list")

    def test_platform_appendices_are_appended_for_one_platform_only(self):
        roles = self.root / ".agents/roles"
        (roles / "coder.claude.md").write_text("Claude-only rule.\n", encoding="utf-8")
        (roles / "coder.codex.md").write_text("\nCodex-only rule.\n\n", encoding="utf-8")
        self.assertIn(".claude/agents/rolesync-coder.md", sync(self.root, check=True))
        sync(self.root)
        text = self.claude()
        body = (roles / "coder.md").read_text(encoding="utf-8").strip()
        self.assertIn("Claude-only rule.", text)
        self.assertNotIn("Codex-only rule.", text)
        self.assertLess(text.index(body), text.index("Claude-only rule."))
        self.assertLess(text.index("Claude-only rule."), text.index("Primary workflow"))
        self.assertIn(body + "\n\nClaude-only rule.\n\nPrimary workflow", text)
        instructions = self.codex()["developer_instructions"]
        self.assertIn(body + "\n\nCodex-only rule.\n\nPrimary workflow", instructions)
        self.assertNotIn("Claude-only rule.", instructions)
        self.assertNotIn("Claude-only rule.", self.claude("rolesync-reviewer"))
        self.assertEqual(sync(self.root, check=True), [])
        (roles / "coder.claude.md").write_text("Claude-only rule, revised.\n", encoding="utf-8")
        self.assertEqual(sync(self.root, check=True), [".claude/agents/rolesync-coder.md", ".agents/generated-manifest.json"])

    def test_empty_appendix_adds_nothing(self):
        (self.root / ".agents/roles/coder.claude.md").write_text("\n\n", encoding="utf-8")
        self.assertEqual(sync(self.root, check=True), [])

    def test_symlinked_appendix_is_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        target = Path(self.temp.name) / "outside.md"
        target.write_text("Injected.\n", encoding="utf-8")
        link = self.root / ".agents/roles/coder.claude.md"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation not permitted: {exc}")
        with self.assertRaisesRegex(RoleSyncError, "appendix must be a normal file"):
            render(self.root)

    # --- backward compatibility -----------------------------------------------

    def test_full_legacy_shape_renders_every_key_in_the_original_order(self):
        fm = frontmatter(self.claude())
        self.assertEqual(
            list(fm),
            ["name", "description", "model", "tools", "permissionMode", "maxTurns", "skills", "effort", "isolation"],
        )
        cx = self.codex()
        self.assertEqual(
            list(cx),
            ["name", "description", "model", "model_reasoning_effort", "sandbox_mode", "developer_instructions"],
        )


if __name__ == "__main__":
    unittest.main()
