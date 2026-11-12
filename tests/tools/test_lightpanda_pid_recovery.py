import json
import os

import pytest

from tools import browser_lightpanda as lp
from tools.process_registry import ProcessRegistry


@pytest.mark.parametrize("field", ["owner_pid", "pid"])
def test_bad_pid_is_preserved_and_does_not_abort_sweep(tmp_path, monkeypatch, field):
    import gateway.status

    monkeypatch.setattr(lp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway.status, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(lp, "_is_lightpanda_process", lambda *args: True)
    killed = []
    monkeypatch.setattr(ProcessRegistry, "_terminate_host_pid", lambda pid, expected_start: killed.append(pid))
    record = {"owner_pid": 20001, "pid": 20002, "port": 9222, "start_time": 3}
    bad, good = tmp_path / "a.json", tmp_path / "b.json"
    bad.write_text(json.dumps({**record, field: "bad"}), encoding="utf-8")
    good.write_text(json.dumps(record), encoding="utf-8")

    assert lp.reap_orphaned_lightpanda() == 1
    assert bad.exists() and not good.exists()
    assert killed == [20002]


def test_current_owner_is_not_reaped_before_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(lp, "_servers", {})
    monkeypatch.setattr(lp, "_is_lightpanda_process", lambda *args: True)
    killed = []
    monkeypatch.setattr(ProcessRegistry, "_terminate_host_pid", lambda *args, **kwargs: killed.append(args))
    record = tmp_path / "starting.json"
    record.write_text(json.dumps({"owner_pid": os.getpid(), "pid": 20002}), encoding="utf-8")

    assert lp.reap_orphaned_lightpanda() == 0
    assert record.exists() and not killed


@pytest.mark.parametrize("owner", [None, 0, -2, True, 1.5, "9" * 100, 2**31])
def test_unusable_owner_is_preserved(tmp_path, monkeypatch, owner):
    monkeypatch.setattr(lp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(lp, "_is_lightpanda_process", lambda *args: True)
    killed = []
    monkeypatch.setattr(ProcessRegistry, "_terminate_host_pid", lambda *args, **kwargs: killed.append(args))
    record = tmp_path / "unknown.json"
    record.write_text(json.dumps({"owner_pid": owner, "pid": 20002}), encoding="utf-8")

    assert lp.reap_orphaned_lightpanda() == 0
    assert record.exists() and not killed


def test_unavailable_owner_check_preserves_record(tmp_path, monkeypatch):
    import gateway.status

    monkeypatch.setattr(lp, "_state_dir", lambda: tmp_path)
    def failed_check(pid):
        raise PermissionError("synthetic")
    monkeypatch.setattr(gateway.status, "_pid_exists", failed_check)
    record = tmp_path / "unknown.json"
    record.write_text(json.dumps({"owner_pid": 20001, "pid": 20002}), encoding="utf-8")

    assert lp.reap_orphaned_lightpanda() == 0
    assert record.exists()


def test_non_object_record_does_not_abort_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "_state_dir", lambda: tmp_path)
    record = tmp_path / "unknown.json"
    record.write_text('"not an object"', encoding="utf-8")
    assert lp.reap_orphaned_lightpanda() == 0
    assert record.exists()
