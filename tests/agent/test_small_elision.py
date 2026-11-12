import json

import pytest

from agent.tool_dispatch_helpers import make_tool_result_message


@pytest.mark.parametrize('payload', [
    {'results': [], 'has_more': True},
    {'result': '', 'structuredContent': {'items': [], 'has_more': True}},
    {'result': '{"items": [], "has_more": true}'},
])
def test_short_paginated_mcp_results_warn_in_actual_message(payload):
    msg = make_tool_result_message('mcp_example_search', json.dumps(payload), 'call1')
    assert 'INCOMPLETE' in msg['content']
    assert 'has_more' in msg['content']


@pytest.mark.parametrize('payload', [
    {'results': [], 'has_more': False},
    {'results': [], 'has_more': 'true'},
    {'result': 'The example {"has_more": true} is documentation.'},
])
def test_short_non_paginated_results_do_not_warn(payload):
    msg = make_tool_result_message('mcp_example_search', json.dumps(payload), 'call1')
    assert 'INCOMPLETE' not in msg['content']


def test_trusted_tool_not_annotated():
    value = '{"results": [], "has_more": true}'
    assert make_tool_result_message('terminal', value, 'call1')['content'] == value
