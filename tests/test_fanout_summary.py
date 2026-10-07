import unittest

from digest import (
    build_digest,
    build_digest_html,
    build_layered_summary_md,
    group_fanout_themes,
    normalize_subject_key,
    split_summary_rows_top_n,
)


def _commit(msg: str, sha: str = "abc1234") -> dict[str, str]:
    return {"sha": sha, "message": msg, "author": "alice", "date": "2026-10-01"}


class NormalizeSubjectKeyTests(unittest.TestCase):
    def test_strips_conventional_prefix_and_issue_key(self) -> None:
        a = "feat(observability): enable Cloudflare Workers Issues (WW-45)"
        b = "fix: enable Cloudflare Workers Issues"
        self.assertEqual(normalize_subject_key(a), normalize_subject_key(b))

    def test_strips_trailing_pr_ref(self) -> None:
        self.assertEqual(
            normalize_subject_key("ci: zizmor secrets-inherit (#99)"),
            normalize_subject_key("ci: zizmor secrets-inherit"),
        )

    def test_strips_fullwidth_repo_qualifier_not_ascii_theme(self) -> None:
        base = "feat(observability): 开启 Cloudflare Workers Issues (WW-45)"
        pilot = "feat(observability): 开启 Cloudflare Workers Issues（sch1 试点）(WW-45)"
        self.assertEqual(normalize_subject_key(base), normalize_subject_key(pilot))
        self.assertIn("zizmor", normalize_subject_key("ci: map secrets (zizmor secrets-inherit)."))

    def test_does_not_merge_unrelated(self) -> None:
        self.assertNotEqual(
            normalize_subject_key("feat: add login"),
            normalize_subject_key("feat: add billing"),
        )


class GroupFanoutThemesTests(unittest.TestCase):
    def test_merges_same_subject_across_repos(self) -> None:
        msg = "feat(observability): 开启 Cloudflare Workers Issues (WW-45)"
        sections = [
            (f"repo-{i}", [_commit(msg, sha=f"{i:07d}")], [])
            for i in range(31)
        ]
        themes = group_fanout_themes(sections)
        self.assertEqual(len(themes), 1)
        self.assertEqual(themes[0].repo_count, 31)
        self.assertEqual(themes[0].commit_count, 31)

    def test_single_repo_not_folded(self) -> None:
        sections = [("only-one", [_commit("feat: unique work")], [])]
        self.assertEqual(group_fanout_themes(sections), [])


class SplitSummaryRowsTests(unittest.TestCase):
    def test_top_n_and_overflow_counts(self) -> None:
        rows = [(f"r{i}", i, 0, "0.0%") for i in range(10, 0, -1)]
        top, extra_repos, extra_commits, _tags = split_summary_rows_top_n(rows, n=8)
        self.assertEqual(len(top), 8)
        self.assertEqual(extra_repos, 2)
        self.assertEqual(extra_commits, 2 + 1)


class LayeredSummaryTests(unittest.TestCase):
    def test_md_includes_themes_and_full_detail_table(self) -> None:
        msg = "ci: map worker-ci secrets explicitly (zizmor secrets-inherit)."
        sections = [(f"r{i}", [_commit(msg, sha=f"{i:07d}")], []) for i in range(5)]
        summary_rows = [(name, 1, 0, "0.0%") for name, _, _ in sections]
        md = build_layered_summary_md(
            "2026-09-28",
            "2026-10-06",
            "UTC",
            summary_rows,
            sections,
            scope="org:workers-world",
        )
        self.assertIn("### Cross-repo themes", md)
        self.assertIn("**5** repos", md)
        self.assertIn("## Full detail", md)
        self.assertIn("### All repositories", md)
        self.assertEqual(md.count("| r0 |"), 2)  # top + full table

    def test_html_summary_only_no_per_repo_dump(self) -> None:
        msg = "feat(observability): WW-45 fan-out"
        sections = [(f"r{i}", [_commit(msg, sha=f"{i:07d}")], []) for i in range(3)]
        html = build_digest_html("org:o", "2026-09-28", "2026-10-06", "UTC", sections)
        self.assertIn("Cross-repo themes", html)
        self.assertIn(">Theme</th>", html)
        self.assertIn(">Authors</th>", html)
        self.assertIn("WW-45 fan-out", html)
        self.assertIn("summary view only", html)
        self.assertNotIn("### Commits", html)
        self.assertNotIn(">r0</h2>", html)

    def test_digest_md_keeps_repo_sections_after_full_detail(self) -> None:
        sections = [
            ("alpha", [_commit("feat: one-off")], []),
            ("beta", [_commit("feat: other")], []),
        ]
        text = build_digest("org:o", "2026-09-28", "2026-10-06", "UTC", sections)
        full_pos = text.index("## Full detail")
        alpha_pos = text.index("## alpha")
        self.assertLess(full_pos, alpha_pos)


if __name__ == "__main__":
    unittest.main()
