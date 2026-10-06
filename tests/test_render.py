import unittest

from digest import (
    build_digest,
    build_digest_csv,
    build_digest_html,
    build_summary_rows,
    build_summary_text,
    format_noise_ratio,
    org_wide_noise_ratio,
    sort_sections,
    truncate_subject,
)


SECTIONS = [
    (
        "alpha",
        [
            {"sha": "abc1234", "message": "fix: short", "author": "alice", "date": "2026-10-06"},
            {
                "sha": "def5678",
                "message": "feat: " + ("x" * 100),
                "author": "bob",
                "date": "2026-10-05",
            },
        ],
        [{"name": "v1.0.0", "sha": "abc1234", "date": "2026-10-06"}],
    ),
    (
        "beta",
        [{"sha": "1111111", "message": "docs: readme", "author": "carol", "date": "2026-10-04"}],
        [],
    ),
    ("quiet", [], []),
]


class TruncateSubjectTests(unittest.TestCase):
    def test_short_unchanged(self) -> None:
        self.assertEqual(truncate_subject("fix: ok"), "fix: ok")

    def test_long_truncated_with_ellipsis(self) -> None:
        long = "a" * 100
        out = truncate_subject(long, max_len=20)
        self.assertLessEqual(len(out), 20)
        self.assertTrue(out.endswith("…"))


class SortSectionsTests(unittest.TestCase):
    def test_orders_by_commits_then_tags_then_name(self) -> None:
        ordered = [name for name, _, _ in sort_sections(SECTIONS)]
        self.assertEqual(ordered, ["alpha", "beta"])


class BuildDigestTests(unittest.TestCase):
    def test_summary_at_top_with_counts_and_window(self) -> None:
        text = build_digest("org:o", "2026-09-29", "2026-10-06", "Asia/Shanghai", SECTIONS)
        summary_pos = text.index("## Summary")
        alpha_pos = text.index("## alpha")
        self.assertLess(summary_pos, alpha_pos)
        self.assertIn("Window: **2026-09-29 .. 2026-10-06** (Asia/Shanghai)", text)
        self.assertIn("| alpha | 2 | 1 | 0.0% |", text)
        self.assertIn("| beta | 1 | 0 | 0.0% |", text)
        self.assertNotIn("## quiet", text)

    def test_summary_table_sorted_by_commit_count_desc(self) -> None:
        sections = [
            ("zeta", [{"sha": "1", "message": "m", "author": "a", "date": "2026-10-01"}], []),
            (
                "alpha",
                [
                    {"sha": "2", "message": "m", "author": "a", "date": "2026-10-02"},
                    {"sha": "3", "message": "m", "author": "a", "date": "2026-10-03"},
                ],
                [],
            ),
            ("beta", [], [{"name": "v1", "sha": "abc", "date": "2026-10-04"}]),
        ]
        text = build_digest("org:o", "2026-09-29", "2026-10-06", "UTC", sections)
        alpha_row = text.index("| alpha | 2 | 0 | 0.0% |")
        zeta_row = text.index("| zeta | 1 | 0 | 0.0% |")
        beta_row = text.index("| beta | 0 | 1 | 0.0% |")
        self.assertLess(alpha_row, zeta_row)
        self.assertLess(zeta_row, beta_row)

    def test_commits_before_tags_and_truncated_subject(self) -> None:
        text = build_digest("org:o", "2026-09-29", "2026-10-06", "UTC", SECTIONS)
        self.assertIn("### Commits (2)", text)
        self.assertLess(text.index("### Commits"), text.index("### Tags"))
        self.assertIn("feat: " + "x" * 60, text)
        self.assertNotIn("x" * 100, text)

    def test_build_summary_text_totals(self) -> None:
        summary = build_summary_text(
            "2026-09-29",
            "2026-10-06",
            "UTC",
            [("alpha", 2, 1, "0.0%"), ("beta", 1, 0, "0.0%")],
        )
        self.assertIn("Repos with activity: **2** · Commits: **3** · Tags: **1**", summary)
        self.assertNotIn("Noise:", summary)

    def test_format_noise_ratio(self) -> None:
        self.assertEqual(format_noise_ratio(raw=0, kept=0), "0.0%")
        self.assertEqual(format_noise_ratio(raw=4, kept=1), "75.0%")
        self.assertEqual(format_noise_ratio(raw=3, kept=3), "0.0%")

    def test_summary_with_per_repo_noise_includes_noise_only_repo(self) -> None:
        per_repo = {
            "real": (2, 0, 1, 0),
            "noise-only": (3, 1, 0, 0),
        }
        rows = build_summary_rows([], per_repo_counts=per_repo)
        self.assertEqual(len(rows), 2)
        by_name = {name: (c, t, noise) for name, c, t, noise in rows}
        self.assertEqual(by_name["real"], (1, 0, "50.0%"))
        self.assertEqual(by_name["noise-only"], (0, 0, "100.0%"))
        self.assertEqual(org_wide_noise_ratio(per_repo), "83.3%")

    def test_build_digest_shows_org_noise_when_filter_stats_present(self) -> None:
        sections = [
            (
                "real",
                [{"sha": "abc1234", "message": "feat", "author": "a", "date": "2026-10-06"}],
                [],
            )
        ]
        per_repo = {"real": (2, 0, 1, 0), "noise-only": (1, 0, 0, 0)}
        text = build_digest(
            "org:o",
            "2026-09-29",
            "2026-10-06",
            "UTC",
            sections,
            per_repo_counts=per_repo,
        )
        self.assertIn("Noise: **66.7%**", text)
        self.assertIn("| noise-only | 0 | 0 | 100.0% |", text)
        self.assertNotIn("## noise-only", text)


class BuildDigestCsvTests(unittest.TestCase):
    def test_csv_rows_for_commits_and_tags(self) -> None:
        csv_text = build_digest_csv("2026-09-29", "2026-10-06", "UTC", SECTIONS)
        lines = csv_text.strip().splitlines()
        self.assertTrue(lines[0].startswith("window_since,window_until,timezone,repo,type"))
        self.assertIn("alpha,commit,abc1234,fix: short,alice", csv_text.replace('"', ""))
        self.assertIn("alpha,tag,abc1234,v1.0.0,,2026-10-06", csv_text.replace('"', ""))


class BuildDigestHtmlTests(unittest.TestCase):
    def test_html_summary_table_and_sections(self) -> None:
        html = build_digest_html("org:o", "2026-09-29", "2026-10-06", "Asia/Shanghai", SECTIONS)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<strong>Window:</strong>", html)
        self.assertIn("2026-09-29 .. 2026-10-06 (Asia/Shanghai)", html)
        self.assertIn("<td>alpha</td>", html)
        self.assertIn("<th align=\"right\">Noise</th>", html)
        self.assertIn("Commits (2)", html)
        self.assertIn("<code>abc1234</code>", html)
        self.assertLess(html.index("Commits (2)"), html.index("Tags (1)"))
        self.assertNotIn("quiet", html)

    def test_html_escapes_special_chars(self) -> None:
        sections = [
            (
                "r",
                [{"sha": "abc1234", "message": "fix <script>", "author": "a & b", "date": "2026-10-06"}],
                [],
            )
        ]
        html = build_digest_html("org:o", "2026-09-29", "2026-10-06", "UTC", sections)
        self.assertIn("fix &lt;script&gt;", html)
        self.assertIn("a &amp; b", html)


if __name__ == "__main__":
    unittest.main()
