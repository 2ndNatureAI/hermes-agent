"""Opt-in, payload-free timings for local startup diagnostics."""
import logging
import os
import time
from contextlib import contextmanager

_STAGES = frozenset({'agent_init', 'client_init', 'session_state_init', 'memory_init', 'turn_lease'})
_LOG = logging.getLogger('hermes.startup_timing')


@contextmanager
def stage(name):
    if name not in _STAGES:
        raise ValueError('invalid timing stage')
    if os.environ.get('HERMES_STARTUP_TIMING') != '1':
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        _LOG.info('startup_timing stage=%s elapsed_ms=%d', name, max(0, round((time.monotonic() - started) * 1000)))
