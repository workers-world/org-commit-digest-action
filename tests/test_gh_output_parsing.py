import json
import unittest

from digest import parse_gh_jq_scalar, parse_repo_list_json


class ParseRepoListJsonTests(unittest.TestCase):
    def test_array_of_objects(self) -> None:
        raw = json.dumps([{"name": "a"}, {"name": "b"}])
        self.assertEqual(parse_repo_list_json(raw), ["a", "b"])

    def test_empty_array(self) -> None:
        self.assertEqual(parse_repo_list_json("[]"), [])

    def test_skips_invalid_entries(self) -> None:
        raw = json.dumps([{"name": "ok"}, {}, {"other": "x"}])
        self.assertEqual(parse_repo_list_json(raw), ["ok"])

    def test_non_array_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            parse_repo_list_json('{"name":"solo"}')


class ParseGhJqScalarTests(unittest.TestCase):
    def test_default_branch_line(self) -> None:
        self.assertEqual(parse_gh_jq_scalar("dev_00_01_00\n"), "dev_00_01_00")

    def test_branch_name_with_whitespace(self) -> None:
        self.assertEqual(parse_gh_jq_scalar("  master  \n"), "master")


if __name__ == "__main__":
    unittest.main()
