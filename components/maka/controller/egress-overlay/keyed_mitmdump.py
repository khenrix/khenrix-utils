#!/usr/bin/env python3
"""Relay the brokered key to the mitmproxy addon through inherited fd 3."""

from __future__ import annotations

import os
import resource
import socket
import sys

SOCKET_PATH = "/run/maka-secret/key.sock"
MAX_KEY_BYTES = 16 * 1024


def main() -> None:
    if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
        raise SystemExit("OpenAI egress proxy requires a zero core-dump limit")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(SOCKET_PATH)
        key = bytearray()
        while len(key) < MAX_KEY_BYTES:
            chunk = client.recv(min(4096, MAX_KEY_BYTES - len(key)))
            if not chunk:
                break
            key.extend(chunk)
    except OSError as error:
        raise SystemExit("OpenAI credential broker is unavailable") from error
    finally:
        client.close()
    if not key or len(key) >= MAX_KEY_BYTES or 0 in key:
        raise SystemExit("OpenAI credential broker returned invalid data")

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, key)
    finally:
        os.close(write_fd)
        for index in range(len(key)):
            key[index] = 0
    if read_fd != 3:
        os.dup2(read_fd, 3, inheritable=True)
        os.close(read_fd)
    else:
        os.set_inheritable(3, True)

    args = ["mitmdump"]
    if os.environ.get("MAKA_EVAL_PROXY_TEST_VERBOSE") != "1":
        args.append("--quiet")
    args.extend([
        "--listen-host", "0.0.0.0",
        "--listen-port", "8080",
        "--set", "block_global=false",
        "--set", "rawtcp=false",
        "--set", "confdir=/opt/maka-egress-state",
        "--scripts", "/opt/maka-eval/egress_filter.py",
        *sys.argv[1:],
    ])
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
