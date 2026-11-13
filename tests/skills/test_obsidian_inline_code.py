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


def test_table_rows_and_cells_are_inline_code_boundaries(tmp_path):
    raw = (
        b'| ` [[Header]] | x |\n| --- | --- |\n| [[LaterRow]] ` | x |\n'
        b'| ` [[CellOne]] | [[CellTwo]] ` |\n'
        b'| `inside [[Hidden]]` | [[Visible]] |\n'
        b'| `code \\| [[HiddenEscapedPipe]]` | [[VisibleAgain]] |\n'
        b'\nOrdinary `code | [[HiddenOrdinaryPipe]]` [[Outside]]\n'
    )
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [
        (1, 'Header'), (3, 'LaterRow'), (4, 'CellOne'), (4, 'CellTwo'),
        (5, 'Visible'), (6, 'VisibleAgain'), (8, 'Outside')
    ]


def test_table_with_no_outer_pipes_keeps_cells_separate(tmp_path):
    raw = b'` [[Header]] | column\n--- | ---\n[[First]] ` | column\n'
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [(1, 'Header'), (3, 'First')]


def test_single_column_aligned_table_without_outer_divider_pipes(tmp_path):
    for divider in (':---:', ':---', '---:', '---'):
        raw = f'| ` [[Header]] |\n{divider}\n| [[Body]] ` |\n'.encode()
        (tmp_path / 'A.md').write_bytes(raw)
        report = checker.inspect(tmp_path, complete=True)
        assert [(f['line'], f['target']) for f in report['findings']] == [(1, 'Header'), (3, 'Body')]


def test_aligned_line_without_header_pipe_remains_ordinary_paragraph(tmp_path):
    raw = b'No table ` intro\n:---:\n[[Hidden]] end` [[Visible]]\n'
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [(3, 'Visible')]


def test_unrecognized_pipe_line_limits_multiline_but_not_same_line_code(tmp_path):
    raw = (
        b'Paragraph `starts\nunknown | [[Visible]]\nends`\n\n'
        b'Ordinary `code | [[Hidden]]` [[AlsoVisible]]\n'
    )
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [(2, 'Visible'), (5, 'AlsoVisible')]


def test_plain_blockquote_multiline_inline_code_hides_links_within_same_paragraph(tmp_path):
    raw = (b'> `sample\n> [[Hidden]] end` [[Visible]]\n'
           b'> ordinary ``sample\n> [[AlsoHidden]] end`` [[AlsoVisible]]\n')
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [(2, 'Visible'), (4, 'AlsoVisible')]
    clean = checker.visible(raw.decode())
    assert len(clean) == len(raw)
    assert [i for i, c in enumerate(clean) if c == '\n'] == [i for i, c in enumerate(raw.decode()) if c == '\n']


def test_blockquote_boundaries_preserve_links_in_separate_blocks(tmp_path):
    raw = (b'> unmatched `\n>\n> [[NewParagraph]] `\n'
           b'> unmatched `\n> # [[Heading]] `\n'
           b'> unmatched `\n> - [[ListItem]] `\n'
           b'> unmatched `\n> > [[NestedQuote]] `\n'
           b'> unmatched `\n[[OutsideQuote]] `\n'
           b'> unmatched `\n> line | [[PipeLine]] `\n')
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [
        (3, 'NewParagraph'), (5, 'Heading'), (7, 'ListItem'),
        (9, 'NestedQuote'), (11, 'OutsideQuote'), (13, 'PipeLine')
    ]


def test_quoted_setext_headings_do_not_join_later_inline_code(tmp_path):
    raw = (b'> ` [[HeadingOne]]\n> ===\n> later ` [[AfterOne]]\n'
           b'\n> ` [[HeadingTwo]]\n> ---\n> later ` [[AfterTwo]]\n')
    (tmp_path / 'A.md').write_bytes(raw)
    report = checker.inspect(tmp_path, complete=True)
    assert [(f['line'], f['target']) for f in report['findings']] == [
        (1, 'HeadingOne'), (3, 'AfterOne'), (5, 'HeadingTwo'), (7, 'AfterTwo')
    ]
