from pathlib import Path
from unittest.mock import patch

from tools import browser_lightpanda as lp


def test_tail_reads_a_bounded_amount_from_large_log(tmp_path):
    log_path = tmp_path / "stderr.log"
    log_path.write_bytes(b"x" * (2 * 1024 * 1024) + b"\nlast error\n")
    original_open = open
    reads = []

    class ObservedFile:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def read(self, size=-1):
            reads.append(size)
            return self.wrapped.read(size)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def observed_open(path, *args, **kwargs):
        opened = original_open(path, *args, **kwargs)
        return ObservedFile(opened) if Path(path) == log_path else opened

    with patch("builtins.open", side_effect=observed_open):
        assert lp._read_log_tail(str(log_path)) == "last error"
    assert reads and all(0 <= size <= lp._STDERR_TAIL_LIMIT for size in reads)


def test_tail_handles_invalid_utf8_and_missing_file(tmp_path):
    log_path = tmp_path / "stderr.log"
    log_path.write_bytes(b"first\nlast\xff\n\n")
    assert lp._read_log_tail(str(log_path)) == "last\ufffd"
    assert lp._read_log_tail(str(tmp_path / "missing")) == ""
