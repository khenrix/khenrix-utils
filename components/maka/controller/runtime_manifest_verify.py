#!/usr/bin/env python3
"""Render the immutable runtime's ordered GNU sha256sum manifest."""

import concurrent.futures
import os
import pathlib
import stat
import subprocess
import sys


WORKERS = 4
BATCH_FILES = 128
BATCH_ARG_BYTES = 64 * 1024
ROOT_MANIFESTS = {b"./RUNTIME_SHA256SUMS", b"./RUNTIME_SYMLINKS"}


class ManifestUnavailable(Exception):
    pass


def regular_paths(root: pathlib.Path) -> list[bytes]:
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise ManifestUnavailable
    pending = [(os.fsencode(root), b".")]
    files = []
    while pending:
        directory, relative = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = os.path.join(directory, entry.name)
                name = b"./" + entry.name if relative == b"." else relative + b"/" + entry.name
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    pending.append((path, name))
                elif stat.S_ISREG(mode):
                    if name not in ROOT_MANIFESTS:
                        files.append(name)
                elif not stat.S_ISLNK(mode):
                    raise ManifestUnavailable
    if not files:
        raise ManifestUnavailable
    return sorted(files)


def batches(paths: list[bytes]):
    batch = []
    argument_bytes = 0
    for path in paths:
        size = len(path) + 1
        if batch and (len(batch) == BATCH_FILES or argument_bytes + size > BATCH_ARG_BYTES):
            yield batch
            batch = []
            argument_bytes = 0
        batch.append(path)
        argument_bytes += size
    if batch:
        yield batch


def hash_batch(root: pathlib.Path, hash_command: str, paths: list[bytes]) -> bytes:
    result = subprocess.run(
        [hash_command, b"--", *paths], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode != 0:
        raise ManifestUnavailable
    return result.stdout


def render_manifest(root: pathlib.Path, hash_command: str) -> bytes:
    paths = regular_paths(root)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outputs = list(pool.map(
            lambda batch: hash_batch(root, hash_command, batch), batches(paths)
        ))
    return b"".join(outputs)


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or not all(os.path.isabs(item) for item in arguments):
        print("runtime manifest verification unavailable", file=sys.stderr)
        return 64
    try:
        manifest = render_manifest(pathlib.Path(arguments[0]), arguments[1])
        sys.stdout.buffer.write(manifest)
        return 0
    except (OSError, ManifestUnavailable):
        print("runtime manifest verification unavailable", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
