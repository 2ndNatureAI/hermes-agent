"""Code spans may cross lines but must not consume separate Markdown blocks."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/note-taking/obsidian/scripts/note_check.py'
spec = importlib.util.spec_from_file_location('obsidian_inline_checker', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_multiline_inline_code_and_escaped_backticks_preserve_visible_links(tmp_path):
    raw = (
        b'Intro `code starts\n[[Hidden]] code ends` [[AfterSpan]]\n'
        b'Escaped \\` [[AfterEscaped]]\n'
        b'`literal slash \\` [[AfterClosing]]\n'
    )
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [
        (2, 'AfterSpan'), (3, 'AfterEscaped'), (4, 'AfterClosing')
    ]
    text = raw.decode()
    clean = checker.visible(text)
    assert len(clean) == len(text)
    assert [i for i, c in enumerate(clean) if c == '\n'] == [i for i, c in enumerate(text) if c == '\n']


def test_unmatched_backticks_do_not_mask_other_blocks(tmp_path):
    raw = (
        b'Unmatched ` opener\n\n[[Paragraph]] `\n'
        b'Unmatched ` opener\n# [[Heading]] `\n'
        b'Unmatched ` opener\n```\n[[InFence]]\n```\n[[AfterFence]] `\n'
    )
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [
        (3, 'Paragraph'), (5, 'Heading'), (10, 'AfterFence')
    ]


def test_escaped_tick_runs_and_crlf_keep_original_line_numbers(tmp_path):
    raw = (
        b'Escaped \\` [[Visible]]\r\n\r\n'
        b'Even \\\\` [[Hidden]] ` [[AlsoVisible]]\r\n\r\n'
        b'Partial \\`` [[AlsoHidden]] ` [[LastVisible]]\r\n'
    )
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [
        (1, 'Visible'), (3, 'AlsoVisible'), (5, 'LastVisible')
    ]


def test_escaped_opener_does_not_pair_with_a_later_unmatched_backtick(tmp_path):
    (tmp_path / 'A.md').write_bytes(b'Escaped \\` [[Visible]] unmatched `\n')
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [(1, 'Visible')]
