#!/usr/bin/env python3
"""Model the untrusted task process, which has no broker mount or real key."""

from __future__ import annotations

import json
import os
import pathlib
import resource

provider_names = sorted(name for name in os.environ if name.endswith("_API_KEY"))
visible = os.environ.get("OPENAI_API_KEY", "")
secret_path = pathlib.Path("/run/maka-secret")
if not visible.startswith("maka-decoy-") or provider_names != ["OPENAI_API_KEY"]:
    raise SystemExit("untrusted child provider environment was not decoy-only")
if secret_path.exists():
    raise SystemExit("untrusted child unexpectedly received the secret broker mount")
if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
    raise SystemExit("untrusted child core-dump limit was not zero")

report = pathlib.Path(os.environ["MAKA_EVIDENCE_DIR"]) / "broker-untrusted-child.json"
report.write_text(json.dumps({
    "openaiCredentialClass": "decoy",
    "providerVariables": provider_names,
    "realCredentialInEnvironment": False,
    "secretBrokerMountPresent": False,
    "processRole": "untrusted-task-simulation",
    "coreDumpLimit": "zero-soft-and-hard",
}, indent=2) + "\n")
