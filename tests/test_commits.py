import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import digest
from digest import build_digest, build_gh_json_args, parse_commit_rows


def _commit(sha: str, message: str, date: str, parents: int = 1) -> dict:
    return {
        "sha": sha,
        "parents": [{"sha": f"p{i}"} for i in range(parents)],
        "commit": {
            "message": message,
            "author": {"name": "alice", "date": date},
            "committer": {"name": "alice", "date": date},
        },
    }


UNTIL = datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class GhJsonArgsTests(unittest.TestCase):
    def test_query_params_force_get(self) -> None:
        # Regression: `gh api` defaults to POST when -f fields are present,
        # which made /commits return 404 and every repo report 0 commits.
        args = build_gh_json_args(
            "repos/o/r/commits", paginate=True, params={"sha": "master", "since": "x"}
        )
        self.assertIn("--method", args)
        self.assertEqual(args[args.index("--method") + 1], "GET")
        self.assertIn("--paginate", args)
        self.assertIn("sha=master", args)

    def test_no_params_still_get(self) -> None:
        args = build_gh_json_args("repos/o/r")
        self.assertEqual(args[args.index("--method") + 1], "GET")


class ParseCommitRowsTests(unittest.TestCase):
    def test_basic_row_and_first_line(self) -> None:
        rows = parse_commit_rows(
            [_commit("abcdef1234", "feat: x\n\nbody", "2026-10-05T03:00:00Z")], UNTIL, SHANGHAI
        )
        self.assertEqual(
            rows,
            [{"sha": "abcdef1", "message": "feat: x", "author": "alice", "date": "2026-10-05"}],
        )

    def test_date_label_uses_timezone(self) -> None:
        # 2026-10-04T20:00Z is 2026-10-05 04:00 in Shanghai.
        rows = parse_commit_rows([_commit("a" * 40, "m", "2026-10-04T20:00:00Z")], UNTIL, SHANGHAI)
        self.assertEqual(rows[0]["date"], "2026-10-05")

    def test_skips_merges_by_default(self) -> None:
        data = [
            _commit("1" * 40, "Merge pull request #5", "2026-10-06T01:00:00Z", parents=2),
            _commit("2" * 40, "fix: real work", "2026-10-06T00:00:00Z"),
        ]
        rows = parse_commit_rows(data, UNTIL, SHANGHAI)
        self.assertEqual([r["message"] for r in rows], ["fix: real work"])
        rows = parse_commit_rows(data, UNTIL, SHANGHAI, include_merges=True)
        self.assertEqual(len(rows), 2)

    def test_skips_after_until_and_empty_message(self) -> None:
        data = [
            _commit("1" * 40, "late", "2026-10-06T13:00:00Z"),
            _commit("2" * 40, "", "2026-10-06T00:00:00Z"),
        ]
        rows = parse_commit_rows(data, UNTIL, SHANGHAI)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "")


class ResolveBranchTests(unittest.TestCase):
    def test_uses_name_returned_for_renamed_branch(self) -> None:
        # GitHub answers /branches/master with the renamed branch (dev_00_01_00).
        with mock.patch.object(digest, "run_gh", return_value="dev_00_01_00\n"):
            self.assertEqual(digest.resolve_branch("o", "r", "master"), "dev_00_01_00")

    def test_falls_back_to_default_branch(self) -> None:
        def fake(args):
            if "/branches/" in args[1]:
                raise RuntimeError("404")
            return "dev\n"

        with mock.patch.object(digest, "run_gh", side_effect=fake):
            self.assertEqual(digest.resolve_branch("o", "r", "master"), "dev")


class ListCommitsErrorTests(unittest.TestCase):
    def test_empty_repo_is_empty_list(self) -> None:
        err = RuntimeError("gh api ... failed: gh: Git Repository is empty. (HTTP 409)")
        with mock.patch.object(digest, "gh_json", side_effect=err):
            self.assertEqual(digest.list_commits("o", "r", "master", UNTIL, UNTIL), [])

    def test_other_errors_raise(self) -> None:
        with mock.patch.object(digest, "gh_json", side_effect=RuntimeError("gh: Not Found (HTTP 404)")):
            with self.assertRaises(digest.CommitListError):
                digest.list_commits("o", "r", "master", UNTIL, UNTIL)


class BuildDigestTests(unittest.TestCase):
    def test_commits_section_before_tags(self) -> None:
        text = build_digest(
            "org:o",
            "2026-09-29",
            "2026-10-06",
            "Asia/Shanghai",
            [
                (
                    "repo",
                    [{"sha": "abc1234", "message": "fix", "author": "a", "date": "2026-10-06"}],
                    [{"name": "v1", "sha": "abc1234", "date": "2026-10-06"}],
                )
            ],
        )
        self.assertIn("### Commits (1)", text)
        self.assertLess(text.index("### Commits"), text.index("### Tags"))


if __name__ == "__main__":
    unittest.main()
