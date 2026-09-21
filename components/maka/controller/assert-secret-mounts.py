#!/usr/bin/env python3
"""Assert only the trusted egress proxy receives the secret broker volume."""

from pathlib import Path

import yaml

compose = yaml.safe_load(Path("controller/egress-overlay/docker-compose-egress-proxy.yaml").read_text())
services = compose.get("services", {})
owners: list[str] = []
for service_name, service in services.items():
    for mount in service.get("volumes", []):
        if isinstance(mount, str):
            mount_parts = mount.split(":")
            source = mount_parts[0]
            target = mount_parts[1] if len(mount_parts) > 1 else ""
            read_only = len(mount_parts) == 3 and mount_parts[2] == "ro"
        else:
            source = mount.get("source", "")
            target = mount.get("target", "")
            read_only = mount.get("read_only") is True
        if source == "maka-eval-secret-broker" or target == "/run/maka-secret":
            if source != "maka-eval-secret-broker" or target != "/run/maka-secret":
                raise SystemExit("secret broker mount source/target was ambiguous")
            if not read_only:
                raise SystemExit("secret broker mount must be read-only")
            owners.append(service_name)
if owners != ["maka-eval-mitmproxy"]:
    raise SystemExit(f"secret broker volume owners were not proxy-only: {owners}")
if "maka-eval-secret-broker" not in compose.get("volumes", {}):
    raise SystemExit("secret broker external volume declaration is missing")
secret = compose["volumes"]["maka-eval-secret-broker"]
if secret.get("external") is not True or secret.get("name") != "${MAKA_EVAL_SECRET_VOLUME}":
    raise SystemExit("secret broker volume must be the exact externally supplied run volume")
print("Secret broker compose mount is proxy-only")
