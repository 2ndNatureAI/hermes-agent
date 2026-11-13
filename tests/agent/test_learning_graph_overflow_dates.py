from agent.learning_graph_render import format_date, render_frames


def test_overflow_timeline_describes_actual_member_dates():
    nodes = [{'id': str(year), 'kind': 'skill', 'timestamp': year * 31557600}
             for year in range(50)]
    nodes.append({'id': 'without-date', 'kind': 'memory', 'timestamp': None})
    timestamps = {node['id']: node['timestamp'] for node in nodes}
    result = render_frames({'nodes': nodes}, rows=14, frames=2)
    for bucket in result['buckets']:
        if bucket['label'] == 'undated':
            assert bucket['date'] == 'unknown'
            continue
        values = [timestamps[node['id']] for node in bucket['nodes']]
        if not values:
            assert bucket['date'] == 'unknown'
            continue
        first, last = format_date(min(values)), format_date(max(values))
        assert bucket['date'] == (first if first == last else f'{first} – {last}')
        assert len(bucket['label']) <= 9
