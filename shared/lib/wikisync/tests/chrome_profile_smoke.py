"""Optional host smoke for Windows Chrome profile discovery.

This file deliberately does not match unittest discover's ``test*.py`` pattern. The
receipt-certifying suite is hermetic and must run without skips on macOS, Linux, and
Windows. Run this host-dependent check explicitly when a mounted Windows Chrome profile
is available:

    python3 shared/lib/wikisync/tests/chrome_profile_smoke.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # shared/lib on path
from wikisync.config import _default_chrome_profile  # noqa: E402


class TestRealChromeProfileSmoke(unittest.TestCase):
    def test_it_finds_a_real_profile_when_one_exists(self):
        roots = list(Path("/mnt/c/Users").glob(
            "*/AppData/Local/Google/Chrome/User Data/*/Bookmarks")) \
            if Path("/mnt/c/Users").is_dir() else []
        if not roots:
            self.skipTest("no Windows Chrome profile on this machine")
        self.assertTrue(Path(_default_chrome_profile()).is_file())


if __name__ == "__main__":
    unittest.main()
