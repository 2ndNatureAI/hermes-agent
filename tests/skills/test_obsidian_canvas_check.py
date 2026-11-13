import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / 'skills/note-taking/obsidian/scripts/canvas_check.py'
)


def run(path):
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                            capture_output=True, text=True, timeout=10)
    return result.returncode, json.loads(result.stdout)


def test_valid_canvas_is_read_only_and_leaves_urls_unopened(tmp_path):
    canvas = tmp_path / 'example.canvas'
    data = {'nodes': [{'id': 'a', 'type': 'link', 'url': 'https://example.invalid'},
                      {'id': 'b', 'type': 'file', 'file': '../unread-file.md'}],
            'edges': [{'id': 'edge', 'fromNode': 'a', 'toNode': 'b'}]}
    original = json.dumps(data).encode()
    canvas.write_bytes(original)
    code, result = run(canvas)
    assert code == 0 and result['checks_passed']
    assert result['node_count'] == 2 and result['edge_count'] == 1
    assert canvas.read_bytes() == original
    assert 'not full schema' in result['scope']


def test_duplicate_identifiers_and_dangling_endpoints_are_reported(tmp_path):
    canvas = tmp_path / 'example.canvas'
    canvas.write_text(json.dumps({'nodes': [{'id': 'same'}, {'id': 'same'}],
        'edges': [{'id': 'e', 'fromNode': 'missing', 'toNode': 'same'},
                  {'id': 'e', 'fromNode': '', 'toNode': 'same'}]}), encoding='utf8')
    code, result = run(canvas)
    assert code == 1 and not result['checks_passed']
    assert [e['code'] for e in result['errors']] == [
        'duplicate_id', 'duplicate_id', 'unknown_endpoint', 'missing_endpoint']
    assert '"missing"' not in json.dumps(result)


def test_json_and_input_size_limits(tmp_path):
    canvas = tmp_path / 'example.canvas'
    for bad in ('{"nodes":[],"nodes":[]}', '{"nodes":NaN}', '{'):
        canvas.write_text(bad, encoding='utf8')
        code, result = run(canvas)
        assert code == 2 and result['error'] == 'invalid_or_ambiguous_json'
    canvas.write_bytes(b' ' * (5 * 1024 * 1024 + 1))
    code, result = run(canvas)
    assert code == 2 and result['error'] == 'file_too_large'


def test_empty_canvas_and_bad_structure(tmp_path):
    canvas = tmp_path / 'example.canvas'
    canvas.write_text('{}', encoding='utf8')
    assert run(canvas)[0] == 0
    canvas.write_text('{"nodes":{},"edges":[7]}', encoding='utf8')
    code, result = run(canvas)
    assert code == 1
    assert [e['code'] for e in result['errors']] == ['expected_array', 'expected_object']


def test_findings_are_bounded_and_unreadable_file_is_reported(tmp_path):
    canvas = tmp_path / 'example.canvas'
    canvas.write_text(json.dumps({'nodes': [{} for _ in range(105)]}), encoding='utf8')
    code, result = run(canvas)
    assert code == 1 and result['error_count'] == 105
    assert len(result['errors']) == 100 and result['errors_truncated']
    code, result = run(tmp_path)
    assert code == 2 and result['error'] == 'not_regular_file'
    code, result = run(tmp_path / 'does-not-exist.canvas')
    assert code == 2 and result['error'] == 'unreadable_file'
