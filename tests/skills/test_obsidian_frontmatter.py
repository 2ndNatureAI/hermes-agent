"""Byte fixtures for LF/CRLF frontmatter, content visibility and source custody."""

import codecs
import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills/note-taking/obsidian/scripts/note_check.py"
)
SPEC = importlib.util.spec_from_file_location("obsidian_frontmatter_check", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
inspect, visible = checker.inspect, checker.visible


class FrontmatterTests(unittest.TestCase):
    def check_fixture(self, raw, expected_line):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "A.md"
            path.write_bytes(raw)
            report = inspect(Path(directory))
            self.assertEqual(report["link_count"], 1, report)
            self.assertEqual(
                report["findings"],
                [
                    {
                        "source": "A.md",
                        "line": expected_line,
                        "target": "ActualBodyLink",
                        "status": "unresolved_in_scope",
                        "candidates": [],
                    }
                ],
            )
            self.assertEqual(path.read_bytes(), raw)

    def test_lf_properties_hidden_body_and_line_preserved(self):
        self.check_fixture(
            b'---\nproperty: "[[NotALink]]"\n---\n# Real\n[[ActualBodyLink]]\n', 5
        )

    def test_crlf_properties_hidden_body_and_line_preserved(self):
        self.check_fixture(
            b'---\r\nproperty: "[[NotALink]]"\r\n---\r\n# Real\r\n[[ActualBodyLink]]\r\n',
            5,
        )

    def test_bom_crlf_properties_hidden(self):
        self.check_fixture(
            codecs.BOM_UTF8
            + b'---\r\nproperty: "[[NotALink]]"\r\n---\r\n[[ActualBodyLink]]\r\n',
            4,
        )

    def test_mixed_boundary_newlines_and_offsets(self):
        text = '---\r\nproperty: "[[NotALink]]"\n---\n[[ActualBodyLink]]\r\n'
        clean = visible(text)
        self.assertEqual(len(clean), len(text))
        self.assertEqual(
            [i for i, c in enumerate(clean) if c == "\n"],
            [i for i, c in enumerate(text) if c == "\n"],
        )
        self.assertNotIn("NotALink", clean)
        self.assertIn("[[ActualBodyLink]]", clean)

    def test_unterminated_properties_remain_visible(self):
        self.check_fixture(b'---\r\nproperty: "[[ActualBodyLink]]"\r\n', 2)

    def test_later_delimiter_is_not_frontmatter(self):
        self.check_fixture(b"# Real\r\n---\r\n[[ActualBodyLink]]\r\n---\r\n", 3)

    def test_inline_code_with_nested_backtick_does_not_create_link(self):
        self.check_fixture(b"`` [[NotALink]] ` tail ``\n[[ActualBodyLink]]\n", 2)

    def test_unmatched_opener_does_not_hide_later_code_span(self):
        self.check_fixture(b"` unmatched; `` [[NotALink]] ``\n[[ActualBodyLink]]\n", 2)

    def test_unmatched_backticks_do_not_hide_later_line(self):
        self.check_fixture(b"` unmatched\n[[ActualBodyLink]] `\n", 2)

    def test_comment_delimiter_inside_inline_code_keeps_later_link(self):
        self.check_fixture(b"`%%` [[ActualBodyLink]]\n", 1)

    def test_html_comment_delimiter_inside_inline_code_keeps_later_link(self):
        self.check_fixture(b"`<!--` [[ActualBodyLink]]\n", 1)

    def test_inline_code_and_comment_hide_only_their_contents(self):
        self.check_fixture(b"`` ` %% [[Ignored]] `` %% ` [[AlsoIgnored]] %% [[ActualBodyLink]]\n", 1)


if __name__ == "__main__":
    unittest.main()
