"""Gateway discovery must reach its bounded protocol without running update builds."""

import json
import sys

import pytest

from hermes_cli import venv_sync


@pytest.mark.parametrize("command", [["gateway", "status"], ["gateway", "ensure", "--json"]])
def test_gateway_probe_leaves_pending_build_for_explicit_update(tmp_path, monkeypatch, command):
    import pm
    from hermes_cli import _launchers

    for name in ("HERMES_DISABLE_LAZY_INSTALLS", "HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='probe'\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    pending = tmp_path / "source-completion-pending"
    pending.write_text("build still owed\n")
    monkeypatch.setattr(venv_sync, "completion_pending_path", lambda _root: pending)
    monkeypatch.setattr(pm, "venv_is_current", lambda **_kw: True)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _root: sys.executable)

    def unexpected_build(*_args, **_kwargs):
        pytest.fail("gateway protocol attempted an unbounded update build")

    monkeypatch.setattr(venv_sync, "_finish_source_update", unexpected_build)

    assert venv_sync.prepare_launch(root, command) is None
    assert pending.read_text() == "build still owed\n"
