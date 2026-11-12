"""Session recall stays useful when automation has more matches than the scan window."""
import json

from hermes_state import SessionDB
from tools.session_search_tool import session_search


def test_interactive_hit_survives_cron_flood(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.session_search_tool._DISCOVER_SCAN_LIMIT', 2)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('interactive', source='cli')
        db.append_message('interactive', role='user', content='synthetic orchard rollout decision')
        for index in range(4):
            session_id = f'cron_{index}'
            db.create_session(session_id, source='cron')
            db.append_message(session_id, role='user', content='synthetic orchard rollout status')
        result = json.loads(session_search(query='orchard rollout', sort='newest', limit=1, db=db))
        assert result['success'] is True
        assert result['results'][0]['session_id'] == 'interactive'
    finally:
        db.close()


def test_cron_is_still_returned_when_no_interactive_match(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.session_search_tool._DISCOVER_SCAN_LIMIT', 2)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('only_cron', source='cron')
        db.append_message('only_cron', role='user', content='synthetic orchard rollout status')
        result = json.loads(session_search(query='orchard rollout', sort='newest', limit=1, db=db))
        assert result['success'] is True
        assert result['results'][0]['session_id'] == 'only_cron'
    finally:
        db.close()


def test_capped_candidates_are_reported_even_if_all_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.session_search_tool._DISCOVER_SCAN_LIMIT', 2)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('current', source='cli')
        for _ in range(3):
            db.append_message('current', role='user', content='synthetic orchard rollout decision')
        result = json.loads(session_search(query='orchard rollout', current_session_id='current', limit=2, db=db))
        assert result['results'] == []
        assert result['search_coverage']['cap_reached'] == ['other']
    finally:
        db.close()


def test_cron_search_error_preserves_interactive_hits_and_warns(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('interactive', source='cli')
        db.append_message('interactive', role='user', content='synthetic orchard rollout decision')
        original_search = db.search_messages
        def search_with_cron_failure(*args, **kwargs):
            if kwargs.get('source_filter') == ['cron']:
                raise RuntimeError('synthetic failure')
            return original_search(*args, **kwargs)
        monkeypatch.setattr(db, 'search_messages', search_with_cron_failure)
        result = json.loads(session_search(query='orchard rollout', limit=2, db=db))
        assert [row['session_id'] for row in result['results']] == ['interactive']
        assert 'Cron search failed' in result['search_warning']
    finally:
        db.close()


def test_hidden_sources_remain_hidden_in_both_lanes(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        for source in ('kanban', 'subagent', 'tool', 'cron'):
            db.create_session(source, source=source)
            db.append_message(source, role='user', content='synthetic orchard rollout decision')
        result = json.loads(session_search(query='orchard rollout', limit=10, db=db))
        assert [row['session_id'] for row in result['results']] == ['cron']
    finally:
        db.close()
