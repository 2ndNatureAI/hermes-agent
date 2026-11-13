import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / 'skills/note-taking/obsidian/scripts/note_check.py'
)
spec = importlib.util.spec_from_file_location('note_check_table', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_table_alias_and_anchor_are_resolved(tmp_path):
    (tmp_path / 'Source.md').write_text(
        '| Link |\n| --- |\n| [[Target#Heading\\|Label]] |\n', encoding='utf8'
    )
    (tmp_path / 'Target.md').write_text('# Heading\n', encoding='utf8')
    report = checker.inspect(tmp_path)
    assert report['link_count'] == 1
    assert report['findings'] == []


def test_alias_escape_does_not_allow_backslash_or_traversal_paths(tmp_path):
    (tmp_path / 'Source.md').write_text(
        '| [[folder\\\\|Label]] | [[../Secret\\|Label]] |\n', encoding='utf8'
    )
    report = checker.inspect(tmp_path)
    assert [f['status'] for f in report['findings']] == [
        'outside_scope', 'outside_scope'
    ]


def test_plain_table_alias_resolves_file(tmp_path):
    (tmp_path / 'Source.md').write_text('| [[Target\\|Label]] |\n', encoding='utf8')
    (tmp_path / 'Target.md').write_text('# Target\n', encoding='utf8')
    report = checker.inspect(tmp_path)
    assert report['link_count'] == 1
    assert report['findings'] == []
