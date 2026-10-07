import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from digest import (
    SummaryTrendContext,
    bucket_commit_type,
    build_commit_type_histogram,
    build_digest,
    build_digest_html,
    build_kpi_snapshot,
    build_layered_summary_md,
    build_sparkline_series,
    build_trend_datasets,
    count_kept_rows_from_csv,
    find_prior_week_kpi,
    format_wow_suffix_html,
    format_wow_suffix,
    kpi_from_meta_record,
    load_kpi_history,
    render_trend_chart_svg,
    select_prior_week_kpi,
    sparkline_ascii,
    sparkline_svg_data_uri,
    summarize_noise_rules,
    wow_delta_pct,
    write_meta,
    history_meta_path,
)

FIXTURE_HISTORY = Path(__file__).resolve().parent / "fixtures" / "history_batch"


class WowDeltaTests(unittest.TestCase):
    def test_relative_change(self) -> None:
        self.assertAlmostEqual(wow_delta_pct(110, 100), 10.0)
        self.assertAlmostEqual(wow_delta_pct(90, 100), -10.0)
        self.assertEqual(wow_delta_pct(0, 0), 0.0)

    def test_format_suffix_arrows(self) -> None:
        self.assertIn("↑", format_wow_suffix(12.5))
        self.assertIn("↓", format_wow_suffix(-3.0))
        self.assertEqual(format_wow_suffix(0.0), " (→0%)")


class CommitTypeBucketTests(unittest.TestCase):
    def test_conventional_types(self) -> None:
        self.assertEqual(bucket_commit_type("feat(api): add route"), "feat")
        self.assertEqual(bucket_commit_type("fix: bug"), "fix")
        self.assertEqual(bucket_commit_type("chore(deps): bump"), "chore")
        self.assertEqual(bucket_commit_type("ci: workflow"), "ci")
        self.assertEqual(bucket_commit_type("docs: readme"), "docs")
        self.assertEqual(bucket_commit_type("release: dev_00_01_00 → master"), "release")
        self.assertEqual(bucket_commit_type("random subject"), "other")

    def test_histogram(self) -> None:
        sections = [
            (
                "a",
                [
                    {"sha": "1", "message": "feat: x", "author": "a", "date": "2026-10-01"},
                    {"sha": "2", "message": "fix: y", "author": "a", "date": "2026-10-01"},
                    {"sha": "3", "message": "no prefix", "author": "a", "date": "2026-10-01"},
                ],
                [],
            )
        ]
        hist = build_commit_type_histogram(sections)
        self.assertEqual(hist["feat"], 1)
        self.assertEqual(hist["fix"], 1)
        self.assertEqual(hist["other"], 1)


class NoiseRuleSummaryTests(unittest.TestCase):
    def test_groups_by_rule_id(self) -> None:
        ignored = Counter({"R4 lockfile_only": 3, "R1 ci_action_pin_bump": 2})
        summary = summarize_noise_rules(ignored)
        self.assertIn("R1 (2)", summary)
        self.assertIn("R4 (3)", summary)


class HistoryHelperTests(unittest.TestCase):
    def test_thin_meta_infers_kpi_from_csv(self) -> None:
        meta_path = FIXTURE_HISTORY / "weeks" / "2026-09-28_2026-10-06" / ".digest-meta.json"
        if not meta_path.parent.joinpath("digest.csv").is_file():
            self.skipTest("history_batch fixture missing")
        data = {
            "scope": "org:workers-world",
            "window": "2026-09-28..2026-10-06",
            "has-activity": True,
            "active-count": 45,
            "digest-csv-file": str(meta_path.parent / "digest.csv"),
        }
        kpi = kpi_from_meta_record(data, meta_path=meta_path)
        assert kpi is not None
        commits, tags = count_kept_rows_from_csv(meta_path.parent / "digest.csv")
        self.assertEqual(kpi["active-repos"], 45)
        self.assertEqual(kpi["kept-commits"], commits)
        self.assertEqual(kpi["tags"], tags)
        self.assertEqual(commits, 252)
        self.assertEqual(tags, 14)

    def test_batch_week_dirs_and_window_chain_prior(self) -> None:
        if not FIXTURE_HISTORY.is_dir():
            self.skipTest("history_batch fixture missing")
        scope = "org:workers-world"
        history = load_kpi_history(FIXTURE_HISTORY, scope, exclude_window="2026-09-28..2026-10-06")
        self.assertGreaterEqual(len(history), 2)
        prior = find_prior_week_kpi(history, "2026-09-28")
        self.assertIsNotNone(prior)
        self.assertEqual(prior.get("_until"), "2026-09-28")
        self.assertEqual(prior.get("active-repos"), 34)

    def test_slim_seed_meta_window_and_snake_kpi(self) -> None:
        repo_history = Path(__file__).resolve().parent.parent / "digest-history"
        if not repo_history.is_dir():
            self.skipTest("digest-history seed not committed")
        data = {
            "scope": "org:workers-world",
            "window": {"since": "2026-09-21", "until": "2026-09-28"},
            "has-activity": True,
            "active-count": 34,
            "kpi": {
                "active_repos": 34,
                "kept_commits": 325,
                "tags": 4,
                "org_noise_ratio": None,
            },
        }
        kpi = kpi_from_meta_record(data, meta_path=repo_history / "2026-09-28.digest-meta.json")
        assert kpi is not None
        self.assertEqual(kpi["active-repos"], 34)
        self.assertEqual(kpi["kept-commits"], 325)
        self.assertEqual(kpi["tags"], 4)
        self.assertEqual(kpi["_until"], "2026-09-28")
        loaded = load_kpi_history(repo_history, "org:workers-world", exclude_window="2099-01-01..2099-01-08")
        self.assertGreaterEqual(len(loaded), 90)

    def test_load_and_select_prior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp)
            scope = "org:test"
            for until, active in [("2026-09-21", 3), ("2026-09-28", 5)]:
                payload = {
                    "scope": scope,
                    "window": f"2026-09-14..{until}" if until == "2026-09-21" else f"2026-09-21..{until}",
                    "kpi": {
                        "scope": scope,
                        "window": f"2026-09-21..{until}",
                        "active-repos": active,
                        "kept-commits": active * 10,
                        "tags": 1,
                        "noise-pct": 10.0,
                    },
                }
                write_meta(history_meta_path(history, until), payload)
            loaded = load_kpi_history(history, scope, exclude_window="2026-09-28..2026-10-05")
            self.assertEqual(len(loaded), 2)
            prior = select_prior_week_kpi(loaded)
            self.assertEqual(prior.get("active-repos"), 5)


class SparklineTests(unittest.TestCase):
    def test_ascii_pads_to_width(self) -> None:
        self.assertEqual(len(sparkline_ascii([1, 2, 3, 4], width=4)), 4)

    def test_svg_data_uri(self) -> None:
        uri = sparkline_svg_data_uri([1, 5, 2, 8])
        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))

    def test_series_includes_current(self) -> None:
        history = [{"kept-commits": 10}, {"kept-commits": 20}]
        current = {"kept-commits": 30}
        self.assertEqual(build_sparkline_series(history, current, "kept-commits"), [10, 20, 30])


class LayeredSummaryP1Tests(unittest.TestCase):
    def _sections(self) -> list[tuple[str, list[dict[str, str]], list[dict[str, str]]]]:
        return [
            (
                "alpha",
                [{"sha": "abc1234", "message": "feat: one", "author": "a", "date": "2026-10-06"}],
                [],
            )
        ]

    def test_wow_and_types_in_summary(self) -> None:
        sections = self._sections()
        summary_rows = [("alpha", 1, 0, "0.0%")]
        trend = SummaryTrendContext(
            prior_kpi={"active-repos": 2, "kept-commits": 4, "tags": 0, "noise-pct": 20.0},
            history=[{"active-repos": 2, "kept-commits": 4, "tags": 0, "noise-pct": 20.0}],
            current_kpi={"active-repos": 1, "kept-commits": 1, "tags": 0, "noise-pct": 0.0},
            noise_rule_summary="R4 (1)",
        )
        md = build_layered_summary_md(
            "2026-09-28",
            "2026-10-06",
            "UTC",
            summary_rows,
            sections,
            scope="org:o",
            org_noise_ratio="0.0%",
            trend=trend,
            type_histogram=Counter({"feat": 1}),
        )
        self.assertIn("vs last week", md)
        self.assertIn("### Commit types (kept)", md)
        self.assertIn("**feat** 1", md)
        self.assertIn("Trend (", md)

    def test_html_top_table_omits_noise_column(self) -> None:
        html = build_digest_html(
            "org:o",
            "2026-09-28",
            "2026-10-06",
            "UTC",
            self._sections(),
            trend=SummaryTrendContext(
                prior_kpi=None,
                history=[],
                current_kpi={"active-repos": 1, "kept-commits": 1, "tags": 0},
            ),
        )
        self.assertNotIn(">Noise</th>", html)
        self.assertIn("Commit types (kept)", html)
        self.assertIn("<table ", html)
        self.assertIn(">Type</th>", html)

    def test_html_trend_chart_has_date_axis(self) -> None:
        history = [
            {"_until": "2026-09-21", "kept-commits": 40, "active-repos": 5},
            {"_until": "2026-09-28", "kept-commits": 55, "active-repos": 6},
        ]
        current = {"kept-commits": 29, "active-repos": 10}
        dates, commits, repos = build_trend_datasets(
            history, current, current_until="2026-10-06"
        )
        chart = render_trend_chart_svg(dates, commits, repos)
        self.assertIn("<svg", chart)
        self.assertIn("9/28", chart)
        self.assertIn("10/6", chart)
        self.assertIn("Commits", chart)
        self.assertIn("Active repos", chart)

    def test_wow_suffix_html_colors(self) -> None:
        up = format_wow_suffix_html(12.0)
        down = format_wow_suffix_html(-5.0)
        self.assertIn("#1a7f37", up)
        self.assertIn("#cf222e", down)

    def test_full_digest_md_still_has_noise_in_full_detail(self) -> None:
        sections = self._sections()
        per_repo = {"alpha": (2, 0, 1, 0)}
        text = build_digest(
            "org:o",
            "2026-09-28",
            "2026-10-06",
            "UTC",
            sections,
            per_repo_counts=per_repo,
        )
        self.assertIn("| alpha | 1 | 0 | 50.0% |", text)


if __name__ == "__main__":
    unittest.main()
