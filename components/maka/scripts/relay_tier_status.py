#!/usr/bin/env python3
"""Show only the last Maka API response's attested model and processing tier."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

MODELS = {"gpt-6-sol", "gpt-5.6-sol"}
TIERS = {"default", "flex", "priority", "scale", "auto"}
FIELDS = {"schema", "time_utc", "requested_model", "observed_model",
          "requested_service_tier", "observed_service_tier", "http_status"}


class StatusError(RuntimeError):
    pass


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StatusError("duplicate receipt field")
        result[key] = value
    return result


def inspect(path: pathlib.Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"schema": "khenrix-maka-relay-tier-status-v1", "status": "unobserved"}
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
        stat.S_IMODE(parent.st_mode) != 0o700):
        raise StatusError("relay tier receipt is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        file = os.fstat(descriptor)
        if (not stat.S_ISREG(file.st_mode) or file.st_uid != os.getuid() or
            stat.S_IMODE(file.st_mode) != 0o600 or file.st_size > 4096):
            raise StatusError("relay tier receipt is unsafe")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            raise StatusError("relay tier receipt is too large")
        receipt = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError) as error:
        raise StatusError("relay tier receipt is malformed") from error
    finally:
        os.close(descriptor)
    if (not isinstance(receipt, dict) or set(receipt) != FIELDS or
        receipt["schema"] != "khenrix-maka-relay-tier-v1" or
        receipt["requested_model"] not in MODELS or
        receipt["observed_model"] not in MODELS | {None} or
        receipt["requested_service_tier"] != "default" or
        receipt["observed_service_tier"] not in TIERS | {None} or
        type(receipt["http_status"]) is not int or
        not 200 <= receipt["http_status"] <= 599 or
        not isinstance(receipt["time_utc"], str) or
        len(receipt["time_utc"]) > 40):
        raise StatusError("relay tier receipt fields are invalid")
    observed = receipt["observed_service_tier"]
    model = receipt["observed_model"]
    if observed is None or model is None:
        status = "unverified"
    elif observed != "default" or model != receipt["requested_model"]:
        status = "drift"
    else:
        status = "standard"
    return {"schema": "khenrix-maka-relay-tier-status-v1", "status": status,
            "requested_model": receipt["requested_model"], "observed_model": model,
            "requested_service_tier": "default", "observed_service_tier": observed,
            "time_utc": receipt["time_utc"]}


def main() -> int:
    path = pathlib.Path.home() / ".local/state/khenrix-utils/maka/relay-last-tier.json"
    try:
        result = inspect(path)
    except (OSError, StatusError) as error:
        print(f"Maka relay tier status failed: {error}", file=sys.stderr)
        return 78
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "drift" else 0


if __name__ == "__main__":
    raise SystemExit(main())
