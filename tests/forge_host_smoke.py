"""Optional checks against this machine's installed CLI plugin caches.

This file deliberately does not match ``test_*.py``. A missing plugin is a host setup fact,
not a Forge engine failure, so it must not add a skip to the deterministic receipt gate.
Run it explicitly with ``make forge-host-smoke`` on a configured machine.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import refresh  # noqa: E402
from forge import taskbundle  # noqa: E402


def test_installed_plugin_paths_resolve_and_hash():
    installed = [cli for cli in refresh.CLIS if refresh.installed_dirs(cli)]
    if not installed:
        pytest.skip("this host has no installed Khenrix CLI plugin cache")
    for cli in refresh.CLIS:
        assert taskbundle._install_dirs(cli) == sorted(refresh.installed_dirs(cli)), \
            f"{cli}: Forge and refresh disagree about the installed directories"
    for cli in installed:
        assert taskbundle.installed_closure(cli) is not None, \
            f"{cli}: the installed closure cannot be described"
