import csv
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import noise_filter
from noise_filter import WindowItem, classify_item, filter_digest_rows


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _item(
    *,
    repo: str = "deploy-tracker-worker",
    sha: str = "abc1234",
    name: str,
    author: str = "ongoing-z",
    item_type: str = "commit",
) -> WindowItem:
    return WindowItem(repo=repo, item_type=item_type, sha=sha, name=name, author=author)


class ClassifyRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files: dict[tuple[str, str, str], list[str] | None] = {}

    def fetch(self, owner: str, name: str, sha: str) -> list[str] | None:
        return self.files.get((owner, name, sha), [])

    def classify(
        self,
        item: WindowItem,
        *,
        peers: list[WindowItem] | None = None,
        owner: str = "workers-world",
        repo_name: str = "deploy-tracker-worker",
    ) -> str | None:
        return classify_item(
            item,
            owner=owner,
            repo_name=repo_name,
            fetch_files=self.fetch,
            same_repo_commits=peers or [item],
        )

    def test_r1_ci_action_pin(self) -> None:
        item = _item(name="chore(ci): bump worker-actions bundle to actions/v0.2.12")
        self.assertEqual(self.classify(item), "R1 ci_action_pin_bump")

    def test_r2_cf_deps(self) -> None:
        item = _item(name="chore: bump Cloudflare npm deps to latest (#87)")
        self.assertEqual(self.classify(item), "R2 cf_deps_autobump")

    def test_r3_sdk_bump(self) -> None:
        item = _item(name="chore(deps): bump framework_sdk_worker to 0.4.33")
        self.assertEqual(self.classify(item), "R3 sdk_dep_bump")

    def test_r4_lockfile_only(self) -> None:
        item = _item(
            sha="de9edec",
            name="CI action 自动刷新 package-lock（GitHub Packages SDK 0.4.31）",
            author="github-actions[bot]",
        )
        self.files[("workers-world", "deploy-tracker-worker", "de9edec")] = ["package-lock.json"]
        self.assertEqual(self.classify(item), "R4 lockfile_only")

    def test_r5_dependabot(self) -> None:
        item = _item(name="Bump hono from 4.13.2 to 4.13.11 (#43)", author="dependabot[bot]")
        self.assertEqual(self.classify(item), "R5 dep_bot")

    def test_r5_package_json_only(self) -> None:
        item = _item(name="更新依赖版本。", sha="ea869bb")
        self.files[("workers-world", "deploy-tracker-worker", "ea869bb")] = ["package.json"]
        self.assertEqual(self.classify(item), "R5 dep_bot")

    def test_r6_ci_retrigger(self) -> None:
        item = _item(name="ci: retrigger after runner env fix (xz + ~/.local/lib)", author="dev-bot")
        self.assertEqual(self.classify(item), "R6 ci_retrigger")

    def test_r7_tooling_ignore(self) -> None:
        item = _item(name="chore: gitignore 忽略 __pycache__。")
        self.assertEqual(self.classify(item), "R7 tooling_ignore_config")

    def test_r8_wrangler_only(self) -> None:
        item = _item(repo="key1", name="update app config", sha="0d76e66")
        self.files[("workers-world", "key1", "0d76e66")] = ["wrangler.toml"]
        self.assertEqual(
            self.classify(item, owner="workers-world", repo_name="key1"),
            "R8 trivial_app_config",
        )

    def test_r9_release_branch_align(self) -> None:
        item = _item(
            name="fix: align RELEASE_BRANCH with current dev line dev_00_12_00 (#30)",
            author="ZEROWORLD",
        )
        self.assertEqual(self.classify(item), "R9 release_branch_align")

    def test_r10a_empty_release(self) -> None:
        item = _item(name="release: dev_00_15_00 → master (#77)", sha="265d533")
        self.files[("workers-world", "deploy-tracker-worker", "265d533")] = []
        self.assertEqual(self.classify(item), "R10a release_promote_empty")

    def test_r10b_noise_only_release(self) -> None:
        item = _item(name="release: dev_00_14_00 → master (#69)", sha="e764dbc")
        self.files[("workers-world", "deploy-tracker-worker", "e764dbc")] = [".github/workflows/ci.yml"]
        self.assertEqual(self.classify(item), "R10b release_promote_noise_only")

    def test_r10c_duplicate_release(self) -> None:
        release = _item(name="release: dev_00_15_00 → master (#71)", sha="3ac9e4a")
        peer = _item(name="feat(observability): 开启 Cloudflare Workers Issues (WW-45)", sha="44e2578")
        self.files[("workers-world", "deploy-tracker-worker", "3ac9e4a")] = [
            "wrangler.toml",
            "package-lock.json",
        ]
        self.files[("workers-world", "deploy-tracker-worker", "44e2578")] = ["wrangler.toml", "src/x.ts"]
        self.assertEqual(
            self.classify(release, peers=[release, peer]),
            "R10c release_promote_duplicate",
        )

    def test_t1_floating_major_tag(self) -> None:
        item = _item(name="v1", item_type="tag", sha="37f6acc")
        self.assertEqual(self.classify(item), "T1 floating_major_tag")

    def test_keep_semver_tag(self) -> None:
        item = _item(name="actions/v0.2.12", item_type="tag")
        self.assertIsNone(self.classify(item))

    def test_keep_ww45_observability(self) -> None:
        item = _item(name="feat(observability): 开启 Cloudflare Workers Issues (WW-45)")
        self.assertIsNone(self.classify(item))

    def test_keep_zizmor_secrets(self) -> None:
        item = _item(name="ci: map worker-ci secrets explicitly (zizmor secrets-inherit).", author="Cursor Agent")
        self.assertIsNone(self.classify(item))

    def test_keep_cve_human_fix(self) -> None:
        item = _item(name="fix(deps): vitest 升至 ^4.1.11，修复 CVE-2026-0001")
        self.files[("workers-world", "deploy-tracker-worker", item.sha)] = [
            "package.json",
            "package-lock.json",
        ]
        self.assertIsNone(self.classify(item))

    def test_keep_release_with_src(self) -> None:
        item = _item(name="release: dev_00_27_00 → master (#92)", sha="relcode1")
        self.files[("workers-world", "deploy-tracker-worker", "relcode1")] = ["src/auth/login.ts"]
        self.assertIsNone(self.classify(item))

    def test_fail_open_when_files_unavailable(self) -> None:
        item = _item(name="release: dev_00_15_00 → master (#77)", sha="failopen")
        self.files[("workers-world", "deploy-tracker-worker", "failopen")] = None
        self.assertIsNone(self.classify(item))


class FilterDigestRowsTests(unittest.TestCase):
    def test_drops_noise_repo_and_counts(self) -> None:
        sections = [
            (
                "por1",
                [{"sha": "aaaaaaa", "message": "chore(deps): bump framework_sdk_worker to 0.4.33", "author": "x", "date": "2026-10-01"}],
                [],
            ),
            (
                "real-repo",
                [{"sha": "bbbbbbb", "message": "feat: real", "author": "x", "date": "2026-10-01"}],
                [],
            ),
        ]

        def fetch(_o: str, _n: str, _s: str) -> list[str]:
            return []

        filtered, counts = filter_digest_rows(
            "workers-world",
            sections,
            fetch_files=fetch,
            repo_owner_name=lambda org, entry: (org, entry.split("/", 1)[-1]),
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], "real-repo")
        self.assertEqual(counts["R3 sdk_dep_bump"], 1)


class GoldFixtureSamplesTests(unittest.TestCase):
    """One row per ignore rule from commit-digest-ignored.csv (uploaded gold set)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ignored_path = FIXTURES / "commit-digest-ignored-samples.csv"
        if not cls.ignored_path.is_file():
            cls.ignored_path = Path("/home/ubuntu/.cursor/projects/workspace/uploads/commit-digest-ignored_c6e8.csv")
        cls.samples: dict[str, dict[str, str]] = {}
        want_prefixes = (
            "R1 ",
            "R2 ",
            "R3 ",
            "R4 ",
            "R5 ",
            "R6 ",
            "R7 ",
            "R8 ",
            "R9 ",
            "R10a ",
            "R10b ",
            "R10c ",
            "T1 ",
        )
        with cls.ignored_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                reason = row.get("ignore_reason", "")
                for prefix in want_prefixes:
                    if reason.startswith(prefix) and prefix not in cls.samples:
                        cls.samples[prefix.strip()] = row

    def test_gold_samples_present(self) -> None:
        missing = [p for p in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10a", "R10b", "R10c", "T1") if p not in self.samples]
        self.assertEqual(missing, [], f"missing gold samples: {missing}")

    def test_gold_ignored_rows_classify_with_mocks(self) -> None:
        files_map: dict[tuple[str, str, str], list[str]] = {
            ("workers-world", "deploy-tracker-worker", "de9edec"): ["package-lock.json"],
            ("workers-world", "deploy-tracker-worker", "ea869bb"): ["package.json"],
            ("workers-world", "key1", "0d76e66"): ["wrangler.toml"],
            ("workers-world", "deploy-tracker-worker", "265d533"): [],
            ("workers-world", "deploy-tracker-worker", "e764dbc"): [".github/workflows/ci.yml"],
            ("workers-world", "deploy-tracker-worker", "85cbdd5"): ["wrangler.toml"],
            ("workers-world", "deploy-tracker-worker", "ww45peer"): ["wrangler.toml", "src/app.ts"],
        }

        def fetch(owner: str, name: str, sha: str) -> list[str] | None:
            return files_map.get((owner, name, sha), [])

        for prefix, row in self.samples.items():
            if prefix == "T1":
                item = WindowItem(
                    repo=row["repo"],
                    item_type="tag",
                    sha=row["sha"],
                    name=row["name"],
                    author="",
                )
            else:
                item = WindowItem(
                    repo=row["repo"],
                    item_type="commit",
                    sha=row["sha"],
                    name=row["name"],
                    author=row["author"],
                )
            peers = [item]
            if prefix == "R10c":
                peers.append(
                    WindowItem(
                        repo=row["repo"],
                        item_type="commit",
                        sha="ww45peer",
                        name="feat(observability): 开启 Cloudflare Workers Issues (WW-45)",
                        author="ongoing-z",
                    )
                )
            reason = classify_item(
                item,
                owner="workers-world",
                repo_name=row["repo"],
                fetch_files=fetch,
                same_repo_commits=peers,
            )
            self.assertIsNotNone(reason, msg=f"{prefix} expected ignore for {row['name']!r}")
            expected = row["ignore_reason"].split()[0]
            self.assertTrue(reason.startswith(expected), msg=f"{prefix} got {reason}")


class DigestNoiseIntegrationTests(unittest.TestCase):
    def test_build_digest_csv_respects_filtered_sections(self) -> None:
        import digest

        sections = [
            (
                "r",
                [{"sha": "abc1234", "message": "feat", "author": "a", "date": "2026-10-06"}],
                [],
            )
        ]
        csv_text = digest.build_digest_csv("2026-09-29", "2026-10-06", "Asia/Shanghai", sections)
        self.assertIn("abc1234", csv_text)
        self.assertIn("feat", csv_text)

    def test_log_noise_filter_verbose_only_breakdown(self) -> None:
        import digest

        counts: Counter[str] = Counter({"R1 ci_action_pin_bump": 2})
        with mock.patch.object(digest, "eprint") as eprint:
            digest.log_noise_filter_stats(counts, verbose=False)
            self.assertEqual(eprint.call_count, 1)
            digest.log_noise_filter_stats(counts, verbose=True)
            self.assertEqual(eprint.call_count, 3)


if __name__ == "__main__":
    unittest.main()
