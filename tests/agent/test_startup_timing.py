import logging

import unittest
from unittest.mock import patch

from agent.startup_timing import stage


class StartupTimingTest(unittest.TestCase):
    def test_opt_in_fixed_payload_free_output(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertNoLogs('hermes.startup_timing', level=logging.INFO):
                with stage('agent_init'):
                    pass
        secret = 'synthetic-private-token-and-prompt'
        with patch.dict('os.environ', {'HERMES_STARTUP_TIMING': '1'}):
            with self.assertLogs('hermes.startup_timing', level=logging.INFO) as result:
                with stage('client_init'):
                    self.assertTrue(secret)
            self.assertEqual(len(result.output), 1)
            self.assertIn('startup_timing stage=client_init elapsed_ms=', result.output[0])
            self.assertNotIn(secret, result.output[0])
            with self.assertRaises(ValueError):
                with stage(secret):
                    pass
