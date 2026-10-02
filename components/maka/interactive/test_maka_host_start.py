"""Kernel process-start proof used by the interactive judge."""

import os
import sys
import time
import unittest

from maka_host_start import ProcBsdInfo, process_start


class HostStartTest(unittest.TestCase):
    def test_structure_matches_characterized_mac_sdk(self):
        import ctypes

        self.assertEqual(ctypes.sizeof(ProcBsdInfo), 136)

    @unittest.skipUnless(sys.platform == "darwin", "macOS libproc route")
    def test_own_process_start_is_kernel_attested_with_microseconds(self):
        observed = process_start(os.getpid())
        self.assertEqual(observed["pid"], os.getpid())
        self.assertEqual(observed["uid"], os.getuid())
        self.assertLessEqual(observed["seconds"] + observed["microseconds"] / 1_000_000,
                             time.time())
        self.assertGreaterEqual(observed["microseconds"], 0)

    def test_invalid_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            process_start(0)
