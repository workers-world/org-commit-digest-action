import os
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from digest import resolve_window


class ResolveWindowTests(unittest.TestCase):
    TZ = "Asia/Shanghai"

    def _now(self, y: int, m: int, d: int, hour: int = 12) -> datetime:
        return datetime(y, m, d, hour, 0, tzinfo=ZoneInfo(self.TZ))

    def test_scheduled_empty_defaults_to_last_completed_mon_mon(self) -> None:
        # 2026-10-05 is a Monday; cron covers the prior Mon–Mon week ending that day.
        now = self._now(2026, 10, 5, 8)
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}, clear=False):
            _, _, since_label, until_label = resolve_window("", "", self.TZ, now=now)
        self.assertEqual(since_label, "2026-09-28")
        self.assertEqual(until_label, "2026-10-05")

    def test_ad_hoc_empty_defaults_to_monday_through_today(self) -> None:
        # 2026-10-07 is Wednesday; partial week from Monday 10/5 through today.
        now = self._now(2026, 10, 7)
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_NAME", None)
            _, _, since_label, until_label = resolve_window("", "", self.TZ, now=now)
        self.assertEqual(since_label, "2026-10-05")
        self.assertEqual(until_label, "2026-10-07")

    def test_ad_hoc_clamps_future_until_to_today(self) -> None:
        # Chained preview often sets until to next Monday; mid-week that must cap at today.
        now = self._now(2026, 10, 7)
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_NAME", None)
            _, _, since_label, until_label = resolve_window(
                "2026-10-06",
                "2026-10-13",
                self.TZ,
                now=now,
            )
        self.assertEqual(since_label, "2026-10-06")
        self.assertEqual(until_label, "2026-10-07")

    def test_explicit_full_mon_mon_week_unchanged(self) -> None:
        now = self._now(2026, 10, 13)
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_NAME", None)
            _, _, since_label, until_label = resolve_window(
                "2026-09-28",
                "2026-10-06",
                self.TZ,
                now=now,
            )
        self.assertEqual(since_label, "2026-09-28")
        self.assertEqual(until_label, "2026-10-06")

    def test_trend_window_labels_match_resolve_until(self) -> None:
        from digest import build_trend_datasets, format_chart_date_label

        now = self._now(2026, 10, 7)
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_EVENT_NAME", None)
            _, _, _, until_label = resolve_window("2026-10-06", "2026-10-13", self.TZ, now=now)
        history = [{"_until": "2026-09-28", "kept-commits": 1, "active-repos": 1}]
        dates, _, _ = build_trend_datasets(
            history,
            {"kept-commits": 2, "active-repos": 2},
            current_until=until_label,
        )
        self.assertEqual(dates[-1], until_label)
        self.assertEqual(format_chart_date_label(until_label), "10/7")


if __name__ == "__main__":
    unittest.main()
