import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / 'skills' / 'research' / 'grounded-citations' / 'scripts' / 'sources.py'
spec = importlib.util.spec_from_file_location('citation_candidate', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_concurrent_holder_is_not_evicted(tmp_path):
    ledger = tmp_path / 'ledger.json'
    script = '''import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location("holder",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
with m._LedgerLock(Path(sys.argv[2])):
 print("ready",flush=True)
 sys.stdin.readline()
'''
    holder = subprocess.Popen([sys.executable, '-c', script, str(SOURCE), str(ledger)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'ready'
        with pytest.raises(TimeoutError):
            with module._LedgerLock(ledger, timeout=0.1):
                pytest.fail('second writer entered a held critical section')
        assert ledger.with_suffix('.json.lock').exists()
    finally:
        holder.stdin.write('\n')
        holder.stdin.flush()
        holder.communicate(timeout=10)
    assert holder.returncode == 0
    with module._LedgerLock(ledger, timeout=0.1):
        pass


def test_timeout_does_not_remove_preexisting_lock(tmp_path):
    ledger = tmp_path / 'ledger.json'
    lock = ledger.with_suffix('.json.lock')
    lock.write_text('synthetic lock owner', encoding='utf8')
    with pytest.raises(TimeoutError):
        with module._LedgerLock(ledger, timeout=0):
            pytest.fail('writer entered without owning lock')
    assert lock.read_text(encoding='utf8') == 'synthetic lock owner'


def test_rejected_add_preserves_ledger_and_retry_after_release_succeeds(tmp_path, monkeypatch):
    ledger = tmp_path / 'ledger.json'
    module.add_sources(ledger, ['https://original.example'])
    before = ledger.read_bytes()
    lock = ledger.with_suffix('.json.lock')
    lock.write_text('synthetic lock owner', encoding='utf8')
    original = module._LedgerLock
    monkeypatch.setattr(module, '_LedgerLock', lambda path: original(path, timeout=0))
    with pytest.raises(TimeoutError):
        module.add_sources(ledger, ['https://second.example'])
    assert ledger.read_bytes() == before
    assert lock.read_text(encoding='utf8') == 'synthetic lock owner'
    lock.unlink()
    result = module.add_sources(ledger, ['https://second.example'])
    assert result[0]['id'] == 2


def test_cleanup_failure_warns_without_claiming_write_failed(tmp_path, monkeypatch, caplog):
    ledger = tmp_path / 'ledger.json'
    lock = ledger.with_suffix('.json.lock')
    unlink = Path.unlink

    def fail_lock_unlink(path, *args, **kwargs):
        if path == lock:
            raise PermissionError('synthetic failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_lock_unlink)
    result = module.add_sources(ledger, ['https://example.invalid'])
    assert result[0]['id'] == 1
    assert ledger.exists() and lock.exists()
    assert 'lock cleanup could not be verified' in caplog.text
