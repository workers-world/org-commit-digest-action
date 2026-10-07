"""Regression shape checks against uploaded week 2026-09-28..2026-10-06 digest."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from digest import build_digest_html, group_fanout_themes

SAMPLE_MD = Path("/home/ubuntu/.cursor/projects/workspace/uploads/digest_a74e.md")


def parse_digest_md_sections(path: Path) -> list[tuple[str, list[dict[str, str]], list[dict[str, str]]]]:
    text = path.read_text(encoding="utf-8")
    sections: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]] = []
    current: str | None = None
    commits: list[dict[str, str]] = []
    tags: list[dict[str, str]] = []
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            if current and (commits or tags):
                sections.append((current, commits, tags))
            current = line[3:].strip()
            commits, tags = [], []
            continue
        if not current or current == "Summary":
            continue
        if line.startswith("- `") and " → `" in line:
            match = re.match(r"- `([^`]+)` → `([^`]+)` \(([^)]+)\)", line)
            if match:
                tags.append({"name": match.group(1), "sha": match.group(2), "date": match.group(3)})
        elif line.startswith("- `"):
            match = re.match(r"- `([0-9a-f]+)` (.+) — (.+) \(([^)]+)\)", line)
            if match:
                commits.append(
                    {
                        "sha": match.group(1),
                        "message": match.group(2),
                        "author": match.group(3),
                        "date": match.group(4),
                    }
                )
    if current and current != "Summary" and (commits or tags):
        sections.append((current, commits, tags))
    return sections


@unittest.skipUnless(SAMPLE_MD.is_file(), "sample digest.md not available")
class SampleWeek20260928ShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sections = parse_digest_md_sections(SAMPLE_MD)

    def test_top_three_fanout_themes_match_sample_week(self) -> None:
        themes = group_fanout_themes(self.sections)
        self.assertGreaterEqual(len(themes), 3)
        top = themes[:3]
        self.assertEqual(top[0].repo_count, 31)
        self.assertIn("Workers Issues", top[0].display_subject)
        self.assertEqual(top[1].repo_count, 31)
        self.assertIn("upload_source_maps", top[1].display_subject)
        self.assertEqual(top[2].repo_count, 25)
        self.assertIn("zizmor secrets-inherit", top[2].display_subject)

    def test_html_email_is_summary_not_full_dump(self) -> None:
        html = build_digest_html(
            "org:workers-world",
            "2026-09-28",
            "2026-10-06",
            "Asia/Shanghai",
            self.sections,
        )
        self.assertIn("Cross-repo themes", html)
        self.assertLess(len(html.splitlines()), 120)
        self.assertNotIn("deploy-tracker-worker</h2>", html)


if __name__ == "__main__":
    unittest.main()
