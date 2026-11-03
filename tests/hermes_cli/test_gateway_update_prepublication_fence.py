"""A checkout owner fences gateway discovery before its home marker is published.

UpdateLock takes checkout custody first. A separate ensure process can observe
that real kernel lock during the interval before the update marker exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import gateway_runtime, update_lock
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("windows")
REPO_ROOT = Path(__file__).resolve().parents[2]

_HOLDER = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli.update_lock import UpdateLock
root, ready = Path(sys.argv[2]), Path(sys.argv[3])
lock = UpdateLock(path=root / 'not-published', install_root=root)
assert lock.acquire_checkout(root)
ready.write_text('held', encoding='utf-8')
sys.stdin.readline()
lock.release()
"""


@pytest.mark.parametrize("marker_body", [None, b"not a valid marker"])
def test_held_checkout_fences_without_a_live_home_marker(tmp_path, monkeypatch, marker_body):
    install, home = tmp_path / "install", tmp_path / "home"
    (install / ".git").mkdir(parents=True)
    home.mkdir()
    marker = home / update_lock.MARKER_NAME
    if marker_body is not None:
        marker.write_bytes(marker_body)
    checkout_lock_path = update_lock.checkout_lock_path
    monkeypatch.setattr(update_lock, "checkout_lock_path", lambda root=None: checkout_lock_path(root or install))
    ready = tmp_path / "holder-ready"
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", _HOLDER, str(REPO_ROOT), str(install), str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        def holder_ready():
            assert child.poll() is None, "checkout holder exited before readiness"
            return ready.exists()

        wait_until(holder_ready, timeout=15, what="disposable checkout holder")
        assert update_lock.checkout_lock_held(install), "fixture failed to hold the real kernel lock"
        assert update_lock.checkout_lock_held(), "gateway probe was not bound to the fixture checkout"
        lock_bytes = update_lock.checkout_lock_path(install).read_bytes()
        assert gateway_runtime._update_fenced({home}), "missing marker hid an active checkout owner"
        assert update_lock.checkout_lock_path(install).read_bytes() == lock_bytes
    finally:
        if child.poll() is None:
            child.stdin.write("settle\n")
            child.stdin.flush()
        try:
            _, stderr = child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            raise
        assert child.returncode == 0, stderr

    assert not update_lock.checkout_lock_held(install)
    assert not gateway_runtime._update_fenced({home}), "settled owner left a false update fence"
    if marker_body is None:
        assert not marker.exists(), "discovery must not publish or reclaim update markers"
    else:
        assert marker.read_bytes() == marker_body, "discovery rewrote a marker"
