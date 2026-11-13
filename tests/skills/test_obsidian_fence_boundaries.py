"""A fence-like code line with text cannot close a Markdown code block."""
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "skills/note-taking/obsidian/scripts/note_check.py"
spec = importlib.util.spec_from_file_location("obsidian_fence_checker", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_fence_like_code_does_not_create_phantom_links_or_headings(tmp_path):
    (tmp_path / "A.md").write_text("[[B#Invisible]]\n", encoding="utf-8")
    (tmp_path / "B.md").write_text(
        "```md\n``` not a closing fence\n# Invisible\n[[Not a link]]\n```\n[[Actual]]\n",
        encoding="utf-8",
    )
    report = checker.inspect(tmp_path)
    assert report["link_count"] == 2
    assert [(item["target"], item["status"]) for item in report["findings"]] == [
        ("B#Invisible", "missing_heading"),
        ("Actual", "unresolved_in_scope"),
    ]
