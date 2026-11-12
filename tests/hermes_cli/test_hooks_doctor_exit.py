from types import SimpleNamespace
import sys

import pytest

from agent import shell_hooks
from hermes_cli import hooks


@pytest.mark.parametrize('stdout', ['', '{"action":"allow"}'])
def test_doctor_reports_nonzero_smoke_exit(stdout, monkeypatch, capsys):
    spec = SimpleNamespace(event='pre_tool_call', command='synthetic-hook', timeout=3)
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {})
    monkeypatch.setattr(shell_hooks, 'iter_configured_hooks', lambda _: [spec])
    monkeypatch.setattr(shell_hooks, 'script_is_executable', lambda _: True)
    monkeypatch.setattr(shell_hooks, 'allowlist_entry_for', lambda *_: {'approved_at': 'synthetic'})
    monkeypatch.setattr(shell_hooks, 'run_once', lambda *_: {
        'returncode': 7, 'stdout': stdout, 'stderr': '', 'elapsed_seconds': 0.1,
        'error': None, 'timed_out': False, 'parsed': None,
    })
    hooks._cmd_doctor(None)
    output = capsys.readouterr().out
    assert 'exit=7' in output
    assert '1 issue(s) found' in output
    assert 'All shell hooks look healthy' not in output
    assert 'ran clean' not in output
    assert '✓ produced valid JSON' not in output


def test_successful_synthetic_hook_retains_clean_status(monkeypatch, capsys):
    spec = SimpleNamespace(event='pre_tool_call', command='synthetic-hook', timeout=3)
    monkeypatch.setattr(shell_hooks, 'script_is_executable', lambda _: True)
    monkeypatch.setattr(shell_hooks, 'allowlist_entry_for', lambda *_: {'approved_at': 'synthetic'})
    monkeypatch.setattr(shell_hooks, 'run_once', lambda *_: {
        'returncode': 0, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0.1,
        'error': None, 'timed_out': False, 'parsed': None,
    })
    assert hooks._doctor_one(spec, shell_hooks) == 0
    assert 'ran clean' in capsys.readouterr().out


def test_intentional_block_is_not_labeled_a_crash(monkeypatch, capsys):
    spec = SimpleNamespace(event='pre_tool_call', command='synthetic-hook', timeout=3)
    monkeypatch.setattr(shell_hooks, 'script_is_executable', lambda _: True)
    monkeypatch.setattr(shell_hooks, 'allowlist_entry_for', lambda *_: {'approved_at': 'synthetic'})
    monkeypatch.setattr(shell_hooks, 'run_once', lambda *_: {
        'returncode': 2, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0.1,
        'error': None, 'timed_out': False, 'parsed': {'action': 'block'},
    })
    assert hooks._doctor_one(spec, shell_hooks) == 1
    output = capsys.readouterr().out
    assert 'exit=2' in output
    assert 'intended block' in output
    assert 'crash' not in output


def test_doctor_observes_real_synthetic_subprocess_failure(tmp_path, monkeypatch, capsys):
    script = tmp_path / 'synthetic_hook.py'
    script.write_text('import sys\nprint("{}")\nsys.exit(7)\n', encoding='utf-8')
    spec = shell_hooks.ShellHookSpec(event='pre_tool_call', command=f'"{sys.executable}" "{script}"')
    monkeypatch.setattr(shell_hooks, 'script_is_executable', lambda _: True)
    monkeypatch.setattr(shell_hooks, 'allowlist_entry_for', lambda *_: {'approved_at': 'synthetic'})
    assert hooks._doctor_one(spec, shell_hooks) == 1
    output = capsys.readouterr().out
    assert 'exit=7' in output
    assert '✓ produced valid JSON' not in output
