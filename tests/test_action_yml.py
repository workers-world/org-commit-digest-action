import unittest
from pathlib import Path


class ActionYmlNotifyWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.action_yml = (Path(__file__).resolve().parent.parent / "action.yml").read_text(
            encoding="utf-8"
        )

    def test_uses_html_file_not_inline_html(self) -> None:
        self.assertIn("html-file:", self.action_yml)
        self.assertNotIn("id: load-html", self.action_yml)
        self.assertNotIn("html: ${{ steps.load-html.outputs.html }}", self.action_yml)
        self.assertNotIn("\n        html:", self.action_yml)

    def test_meta_exports_digest_html_file_path(self) -> None:
        self.assertIn('"digest-html-file"', self.action_yml)

    def test_notify_passes_csv_attachment_file(self) -> None:
        self.assertIn("attachment-file:", self.action_yml)
        self.assertIn("steps.meta.outputs.digest-csv-file", self.action_yml)
        self.assertIn("attachment-filename: commit-digest.csv", self.action_yml)


if __name__ == "__main__":
    unittest.main()
