import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / 'skills/research/grounded-citations/scripts/sources.py'


def test_cli_keeps_query_values_and_ids_distinct(tmp_path):
    ledger = tmp_path / 'ledger.json'
    command = [sys.executable, str(SCRIPT), '--ledger', str(ledger)]
    a = 'https://example.invalid/page?file=/'
    b = 'https://example.invalid/page?file='
    result = subprocess.run(command + ['add', a, b, a + '#section'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    entries = json.loads(ledger.read_text(encoding='utf-8'))['sources']
    assert [(entry['id'], entry['url']) for entry in entries] == [(1, a), (2, b)]
    draft = tmp_path / 'draft.md'
    draft.write_text('A supported sentence. [1]\n\nSources:\n[1] ' + b + '\n', encoding='utf-8')
    verified = subprocess.run(command + ['verify', str(draft)], capture_output=True, text=True)
    assert verified.returncode != 0
    assert 'does not match' in verified.stdout + verified.stderr


def test_existing_path_and_fragment_normalization_remains(tmp_path):
    ledger = tmp_path / 'ledger.json'
    command = [sys.executable, str(SCRIPT), '--ledger', str(ledger), 'add']
    values = ['https://example.invalid/page', 'https://example.invalid/page/',
              'https://example.invalid/page#section']
    result = subprocess.run(command + values, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(ledger.read_text(encoding='utf-8'))['sources']) == 1
