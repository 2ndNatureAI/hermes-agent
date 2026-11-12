import pytest
from agent.tool_dispatch_helpers import make_tool_result_message


def test_later_text_pagination_warns_and_image_is_unchanged():
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}
    parts = [{'type': 'text', 'text': 'Overview.'}, image,
             {'type': 'text', 'text': '{"items": [], "has_more": true}'}]
    message = make_tool_result_message('mcp_example_search', parts, 'call-1')
    assert 'may be INCOMPLETE' in message['content'][2]['text']
    assert message['content'][1] == image
    assert 'INCOMPLETE' not in parts[2]['text']


def test_later_text_no_warning_for_nonpagination_and_trusted_tool():
    parts = [{'type': 'text', 'text': 'Overview.'}, {'type': 'text', 'text': '{"has_more": false}'}]
    assert all('INCOMPLETE' not in x['text'] for x in make_tool_result_message('mcp_example_search', parts, 'x')['content'])
    assert make_tool_result_message('terminal', parts, 'x')['content'] == parts


def test_scan_limits_parts_and_total_text():
    from agent.tool_dispatch_helpers import _maybe_append_elision_notice
    marker = {'type': 'text', 'text': '{"has_more": true}'}
    numerous = [{'type': 'text', 'text': 'ok'} for _ in range(32)] + [marker]
    assert _maybe_append_elision_notice('mcp_example_search', numerous) == numerous
    large = [{'type': 'text', 'text': 'x' * 65536}, marker]
    assert _maybe_append_elision_notice('mcp_example_search', large) == large


def test_one_notice_for_multiple_matches():
    from agent.tool_dispatch_helpers import _maybe_append_elision_notice
    parts = [{'type': 'text', 'text': '{"has_more": true}'}, {'type': 'text', 'text': '{"has_more": true}'}]
    result = _maybe_append_elision_notice('mcp_example_search', parts)
    assert sum('INCOMPLETE' in part['text'] for part in result) == 1
    assert parts[0]['text'] == '{"has_more": true}'
