"""The repository is a Claude Code plugin and its own marketplace."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))

    def test_plugin_manifest(self):
        self.assertRegex(self.plugin["name"], NAME)
        self.assertRegex(self.plugin["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(self.plugin["description"])
        self.assertEqual(self.plugin["license"], "MIT")

    def test_marketplace_lists_this_plugin_at_the_root(self):
        self.assertRegex(self.marketplace["name"], NAME)
        self.assertTrue(self.marketplace["owner"]["name"])
        entries = {entry["name"]: entry for entry in self.marketplace["plugins"]}
        self.assertEqual(entries[self.plugin["name"]]["source"], "./")
        # One version source: plugin.json, so the two files cannot disagree.
        self.assertNotIn("version", entries[self.plugin["name"]])

    def test_every_skill_is_discoverable(self):
        skills = sorted(path for path in (ROOT / "skills").iterdir() if path.is_dir())
        self.assertEqual(len(skills), 5)
        for directory in skills:
            with self.subTest(skill=directory.name):
                fields = frontmatter(directory / "SKILL.md")
                self.assertEqual(fields.get("name"), directory.name)
                self.assertRegex(directory.name, NAME)
                self.assertTrue(fields.get("description"))
                self.assertLessEqual(len(fields["description"]), 1024)


IMAGE_OR_FONT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".woff", ".woff2", ".ttf", ".otf"}
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"}
RESERVED = {"claude", "anthropic", "official", "plugin", "mcp", "test"}
SKIP = {".git", "__pycache__", ".venv", ".pytest_cache", ".test-output", ".test-deps", "snapshots", "reports"}


def plugin_files():
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if not any(part in SKIP for part in parts):
            yield path


class DirectorySubmissionTests(unittest.TestCase):
    """Blocking and held rules of the Claude plugin directory (see PUBLISHING.md)."""

    def setUp(self):
        self.plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))

    def test_listing_fields(self):
        name = self.plugin["name"]
        self.assertLessEqual(len(name), 64)
        self.assertNotIn(name, RESERVED)
        self.assertTrue(self.plugin["displayName"])
        self.assertTrue(self.plugin["author"]["name"])
        self.assertTrue((ROOT / "LICENSE").is_file())

    def test_readme_has_at_least_40_words_outside_code(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        self.assertGreaterEqual(len(prose.split()), 40)

    def test_file_count_size_and_kinds(self):
        files = [path for path in plugin_files() if path.is_file()]
        self.assertLess(len(files), 512)
        for path in plugin_files():
            relative = path.relative_to(ROOT)
            with self.subTest(path=str(relative)):
                self.assertFalse(path.is_symlink())
                self.assertNotIn(path.name, JUNK)
                self.assertRegex(path.name, r"^[A-Za-z0-9._-]+$")
                self.assertFalse(path.name.endswith((".", " ")))
                if path.is_file() and path.suffix.lower() not in IMAGE_OR_FONT:
                    self.assertLess(path.stat().st_size, 256 * 1024)
        self.assertFalse((ROOT / ".gitattributes").exists())
        self.assertFalse((ROOT / ".gitmodules").exists())

    def test_no_hooks_or_mcp_servers_declared(self):
        # The README's data-handling section states the plugin ships skills only.
        for key in ("hooks", "mcpServers", "experimental"):
            self.assertNotIn(key, self.plugin)
        self.assertFalse((ROOT / "hooks").exists())
        self.assertFalse((ROOT / ".mcp.json").exists())

    def test_only_the_advisory_script_uses_the_network(self):
        users = sorted(str(path.relative_to(ROOT).as_posix()) for path in (ROOT / "skills").rglob("*.py")
                       if re.search(r"^\s*(?:from|import)\s+(?:urllib\.request|http\.client|socket|ssl|ftplib|smtplib|requests)\b",
                                    path.read_text(encoding="utf-8"), re.MULTILINE))
        self.assertEqual(users, ["skills/postgres-security-review/scripts/advisories.py"])


if __name__ == "__main__":
    unittest.main()
