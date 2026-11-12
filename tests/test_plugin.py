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


if __name__ == "__main__":
    unittest.main()
