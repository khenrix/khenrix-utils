#!/usr/bin/env python3
"""Credential-free proof that the Eval child sees only a decoy provider key."""

from __future__ import annotations

import json
import os
import pathlib
import resource
import socket

provider_names = sorted(name for name in os.environ if name.endswith("_API_KEY"))
visible = os.environ.get("OPENAI_API_KEY", "")
if not visible.startswith("maka-decoy-") or provider_names != ["OPENAI_API_KEY"]:
    raise SystemExit("child provider environment was not sanitized")
if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
    raise SystemExit("trusted proxy consumer core-dump limit was not zero")

report = pathlib.Path(os.environ["MAKA_EVIDENCE_DIR"]) / "broker-child.json"
report.write_text(json.dumps({
    "openaiCredentialClass": "decoy",
    "providerVariables": provider_names,
    "realCredentialInEnvironment": False,
    "processRole": "trusted-broker-consumer",
    "coreDumpLimit": "zero-soft-and-hard",
}, indent=2) + "\n")

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect("/run/maka-secret/key.sock")
received = bytearray()
while True:
    chunk = client.recv(4096)
    if not chunk:
        break
    received.extend(chunk)
client.close()
if not received:
    raise SystemExit("broker did not deliver synthetic credential to proxy boundary")
for index in range(len(received)):
    received[index] = 0
