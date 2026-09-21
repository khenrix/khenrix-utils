#!/usr/bin/env python3
"""Emit the explicitly selected Codex Auth key to a disposable process."""
import contextlib
import pathlib
import sys

INTERACTIVE = pathlib.Path(__file__).resolve().parent.parent / "interactive"
SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(INTERACTIVE))
sys.path.insert(0, str(SCRIPTS))

from harden_python_runtime import RuntimeHardeningError, assert_current_runtime  # noqa: E402
from maka_openai_relay import (  # noqa: E402
    KeychainOpenAIKey,
    ProviderCredentialError,
    RelayConfigurationError,
    default_keychain_account_path,
    default_relay_ready_path,
    read_keychain_account,
)
for local_root in (INTERACTIVE, SCRIPTS):
    with contextlib.suppress(ValueError):
        sys.path.remove(str(local_root))

try:
    assert_current_runtime()
    account = read_keychain_account(default_keychain_account_path())
    key = KeychainOpenAIKey(
        account,
        default_keychain_account_path(),
        default_relay_ready_path(),
    )()
except (ProviderCredentialError, RelayConfigurationError, RuntimeHardeningError, OSError) as error:
    raise SystemExit("Selected Codex Auth Keychain entry is unavailable") from error
sys.stdout.write(key + "\n")
