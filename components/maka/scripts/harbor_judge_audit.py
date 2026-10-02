#!/usr/bin/env python3
"""Report only fixed proxy counters after a failed blind judgment."""

import json
import re
from pathlib import Path


SCHEMA = "khenrix-maka-harbor-judge-audit-v1"
MAX_AUDIT_BYTES = 1024 * 1024
RESPONSE_DIAGNOSTIC = re.compile(
    r"status=(0|[1-5][0-9]{2});encoding=(identity|gzip|br|deflate|other);"
    r"reason=(upstream-status|missing-body|oversize-body|invalid-sse|failed-event|"
    r"tool-call|model-mismatch|created-tier|completed-tier|identity-mismatch|"
    r"invalid-completion|missing-completion)\Z"
)


def summarize(root: Path) -> dict[str, object]:
    unavailable = {"schema": SCHEMA, "state": "unavailable"}
    try:
        audit = root / "hits.jsonl"
        if audit.is_symlink():
            return unavailable
        if not audit.exists():
            return {"schema": SCHEMA, "state": "not_recorded"}
        if not audit.is_file() or audit.stat().st_size > MAX_AUDIT_BYTES:
            return unavailable
        counts = {"total_events": 0, "openai_connects": 0,
                  "websocket_probes": 0, "authorized": 0,
                  "request_rejected": 0, "credential_rejected": 0,
                  "judge_rejected": 0, "response_rejected": 0,
                  "response_rejections": [],
                  "authority_mismatch": 0, "raw_tunnel": 0,
                  "policy_error": 0, "other_events": 0}
        for line in audit.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("ruleId"), str):
                return unavailable
            rule = record["ruleId"]
            counts["total_events"] += 1
            if rule == "openai_connect":
                counts["openai_connects"] += 1
            elif rule == "openai_websocket_disabled":
                counts["websocket_probes"] += 1
            elif rule == "openai_authorized":
                counts["authorized"] += 1
            elif rule == "credential_boundary":
                counts["credential_rejected"] += 1
                counts["request_rejected"] += 1
            elif rule == "judge_boundary":
                counts["judge_rejected"] += 1
                counts["request_rejected"] += 1
            elif rule == "judge_response":
                path = record.get("normalizedPath")
                match = RESPONSE_DIAGNOSTIC.fullmatch(path) if isinstance(path, str) else None
                if match is None or counts["response_rejected"] >= 1:
                    return unavailable
                counts["response_rejected"] += 1
                counts["response_rejections"].append({
                    "upstream_status": int(match.group(1)),
                    "content_encoding": match.group(2),
                    "reason": match.group(3),
                })
            elif rule in {"authority_mismatch", "raw_tunnel", "policy_error"}:
                counts[rule] += 1
            else:
                counts["other_events"] += 1
        receipts = root / "relay-judge-receipts"
        if receipts.is_symlink() or (receipts.exists() and not receipts.is_dir()):
            return unavailable
        receipt_count = sum(
            entry.is_file() and not entry.is_symlink() and entry.suffix == ".json"
            for entry in receipts.iterdir()
        ) if receipts.exists() else 0
        return {"schema": SCHEMA, "state": "ok", **counts, "receipt_count": receipt_count}
    except (OSError, UnicodeError, ValueError, TypeError):
        return unavailable


if __name__ == "__main__":
    print(json.dumps(summarize(Path("/opt/maka-egress-state")), sort_keys=True, separators=(",", ":")))
