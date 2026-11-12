"""Subagent lifecycle URL summaries never expose authority or query credentials."""
import pytest
from tools.delegate_tool import _subagent_stop_tool_call_history


@pytest.mark.parametrize(('raw', 'expected'), [
    ('//user:SYNTHETIC_SECRET_421@example.test:8443/path?q=SYNTHETIC_SECRET_421#fragment', '//example.test:8443/path'),
    ('https://user:SYNTHETIC_SECRET_421@example.test:8443/path?q=SYNTHETIC_SECRET_421#fragment', 'https://example.test:8443/path'),
    ('//u:SYNTHETIC_SECRET_421@[::1]:8443/path?key=SYNTHETIC_SECRET_421', '//[::1]:8443/path'),
    ('https://u:SYNTHETIC_SECRET_421@[v1.example]:8443/path?key=SYNTHETIC_SECRET_421', 'https://[v1.example]:8443/path'),
    ('https://user%3ASECRET_MARKER_927%40example.test/p', None),
    ('//user%3ASECRET_MARKER_927%40example.test/p', None),
    ('https://user%253ASECRET_MARKER_927%2540example.test/p', None),
    ('https://bücher.example/p?key=SYNTHETIC_SECRET_421', 'https://bücher.example/p'),
    ('https://[fe80::1%25eth0]/p', None),
    ('/safe/path?q=SYNTHETIC_SECRET_421#frag', '/safe/path'),
    ('example.test/path?token=SYNTHETIC_SECRET_421', None),
    ('https://example.test:bad/path?token=SYNTHETIC_SECRET_421', None),
])
def test_hook_history_url_targets_drop_authority_and_query_secrets(raw, expected):
    trace = [{'tool': 'synthetic', 'status': 'ok', 'input_summary': {
        'argument_keys': ['url'], 'targets': {'url': raw}}}]
    history = _subagent_stop_tool_call_history(trace)
    targets = history[0]['tool_input']['targets']
    assert targets.get('url') == expected
    assert 'SYNTHETIC_SECRET_421' not in str(history)
    assert 'SECRET_MARKER_927' not in str(history)


def test_list_of_urls_keeps_only_safe_targets():
    trace = [{'tool': 'synthetic', 'status': 'ok', 'input_summary': {
        'argument_keys': ['urls'], 'targets': {'urls': [
            '//u:SYNTHETIC_SECRET_421@host.test/path?q=SYNTHETIC_SECRET_421',
            'host.test/path?token=SYNTHETIC_SECRET_421',
            'https://host.test/normal',
        ]}}}]
    history = _subagent_stop_tool_call_history(trace)
    assert history[0]['tool_input']['targets']['urls'] == ['//host.test/path', 'https://host.test/normal']
    assert 'SYNTHETIC_SECRET_421' not in str(history)
    assert 'SECRET_MARKER_927' not in str(history)


def test_exact_url_limit_and_one_over():
    prefix = 'https://example.test/'
    valid = prefix + 'p' * (1024 - len(prefix))
    trace = [{'tool': 'synthetic', 'status': 'ok', 'input_summary': {
        'argument_keys': ['urls'], 'targets': {'urls': [valid, valid + 'p']}}}]
    history = _subagent_stop_tool_call_history(trace)
    assert history[0]['tool_input']['targets']['urls'] == [valid]
