import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.version_info import _run_git


class VersionGitOutputTest(unittest.TestCase):
    def test_output_uses_file_and_timeout_returns_none(self):
        def successful(*args, **kwargs):
            self.assertNotEqual(kwargs['stdout'], subprocess.PIPE)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            kwargs['stdout'].write(b'abc123\n')
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            with patch('hermes_cli.version_info.subprocess.run', side_effect=successful):
                self.assertEqual(_run_git(Path(tmp), 'rev-parse', 'HEAD'), 'abc123')
            with patch('hermes_cli.version_info.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 3)):
                self.assertIsNone(_run_git(Path(tmp), 'rev-parse', 'HEAD'))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows pipe inheritance regression')
    def test_timeout_does_not_wait_for_descendant_output_handle(self):
        real_run = subprocess.run
        code = ("import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(7)']); "
                "time.sleep(15)")
        def substitute(_command, **kwargs):
            return real_run([sys.executable, '-c', code], **kwargs)
        with patch('hermes_cli.version_info.subprocess.run', side_effect=substitute):
            start = time.monotonic()
            self.assertIsNone(_run_git(Path(tempfile.gettempdir()), 'rev-parse', 'HEAD'))
            self.assertLess(time.monotonic() - start, 6)
