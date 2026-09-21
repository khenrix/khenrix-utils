#!/usr/bin/env python3
"""Query the four progressive-disclosure layers of the local memory worker."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import urllib.parse
import urllib.request
from typing import Any

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def worker_port() -> int:
    return int(os.environ.get("KHENRIX_MEMORY_GATEWAY_PORT", "48175"))


def gateway_token() -> str:
    home = pathlib.Path(os.environ.get("AGENTIC_MEMORY_HOME", pathlib.Path.home())).expanduser()
    path = home / ".config" / "agentic-memory" / "gateway-token"
    metadata = path.lstat()
    if not path.is_file() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise RuntimeError(f"owner-only memory gateway token required: {path}")
    return path.read_text(encoding="ascii").strip()


def request_json(path: str, *, body: dict[str, Any] | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{worker_port()}{path}",
        data=payload,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {gateway_token()}",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("memory response exceeded 16 MiB")
    return json.loads(raw)


def _query_path(endpoint: str, values: dict[str, Any]) -> str:
    filtered = {key: value for key, value in values.items() if value is not None}
    return endpoint + "?" + urllib.parse.urlencode(filtered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--project")
    search.add_argument("--type")
    timeline = commands.add_parser("timeline")
    anchor = timeline.add_mutually_exclusive_group(required=True)
    anchor.add_argument("--anchor", type=int)
    anchor.add_argument("--query")
    timeline.add_argument("--before", type=int, default=3)
    timeline.add_argument("--after", type=int, default=3)
    timeline.add_argument("--project")
    observations = commands.add_parser("observations")
    observations.add_argument("ids", nargs="+", type=int)
    tool_uses = commands.add_parser("tool-uses")
    tool_uses.add_argument("ids", nargs="+")
    args = parser.parse_args(argv)

    if args.command == "search":
        result = request_json(
            _query_path(
                "/api/search",
                {"query": args.query, "limit": args.limit, "project": args.project, "type": args.type},
            )
        )
    elif args.command == "timeline":
        result = request_json(
            _query_path(
                "/api/timeline",
                {
                    "anchor": args.anchor,
                    "query": args.query,
                    "depth_before": args.before,
                    "depth_after": args.after,
                    "project": args.project,
                },
            )
        )
    elif args.command == "observations":
        result = request_json("/api/observations/batch", body={"ids": args.ids})
    else:
        parsed_ids: list[int | str] = [int(item) if item.isdigit() else item for item in args.ids]
        result = request_json("/api/tool-uses/batch", body={"ids": parsed_ids})
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
