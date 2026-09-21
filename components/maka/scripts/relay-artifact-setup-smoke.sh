#!/usr/bin/env bash
set -euo pipefail
umask 077
source "$(dirname "$0")/lib.sh"

image=alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74
test "$(docker image inspect "$image" --format '{{.Id}}')" \
  = sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74

install -d -m 0700 "$LAB_ROOT/controller/.build"
logs_root=$(mktemp -d "$LAB_ROOT/controller/.build/relay-logs-XXXXXX")
payload_root=$(mktemp -d "$LAB_ROOT/controller/.build/relay-payloads-XXXXXX")
cleanup() {
  status=$?
  trap - EXIT INT TERM
  chmod -R u+w "$logs_root" 2>/dev/null || status=70
  find "$logs_root" -depth -delete 2>/dev/null || status=70
  chmod -R u+w "$payload_root" 2>/dev/null || status=70
  find "$payload_root" -depth -delete 2>/dev/null || status=70
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Reproduce the Harbor 0.20.0 host-bind incompatibility that the overlay fixes.
set +e
docker run --rm --network none \
  --mount "type=bind,source=$logs_root,target=/logs" \
  --entrypoint /bin/sh "$image" -c \
  'mkdir -p /logs/agent /logs/artifacts && chown 0:0 /logs/agent /logs/artifacts && chmod 700 /logs/agent /logs/artifacts' \
  >/dev/null 2>&1
unpatched_status=$?
set -e
test "$unpatched_status" -ne 0
find "$logs_root" -mindepth 1 -depth -delete

# Execute exactly the root-owner branch emitted by the patched RelayAgent.
docker run --rm --network none \
  --mount "type=bind,source=$logs_root,target=/logs" \
  --entrypoint /bin/sh "$image" -c \
  'set -eu
   owner=$(id -u):$(id -g)
   test "$owner" = 0:0
   mkdir -p /logs/agent /logs/artifacts
   if test "$owner" != 0:0; then chown "$owner" /logs/agent /logs/artifacts; fi
   chmod 700 /logs/agent /logs/artifacts
   test -w /logs/agent
   test -w /logs/artifacts
   : > /logs/agent/setup-probe
   : > /logs/artifacts/setup-probe'

test "$(stat -f '%Lp' "$logs_root/agent")" = 700
test "$(stat -f '%Lp' "$logs_root/artifacts")" = 700
test -f "$logs_root/agent/setup-probe"
test -f "$logs_root/artifacts/setup-probe"

# Docker cp and Harbor's tar fallback both restore uid/gid metadata. The
# patched exact-root Harbor/Unix branch instead sends bytes over compose-exec
# stdin. Exercise the same shell writer in the pinned task image with no
# network and payloads that would be unsafe to interpolate into a command.
python3 - "$payload_root/stdout" "$payload_root/stderr" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_bytes(b"stdout: '$HOME' `literal` \\xff\n\x00tail")
Path(sys.argv[2]).write_bytes("stderr: snowman ☃\n".encode())
PY

ln -s ../agent "$logs_root/artifacts/maka-subject.stdout.txt"

writer_script=$(python3 - "$LAB_ROOT" <<'PY'
import ast
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[1]) / "controller"))
import apply_maka_compat as compat

module = ast.parse(compat.PERSIST_NEW.decode("utf-8"))
scripts = [
    ast.literal_eval(node.value)
    for node in module.body
    if isinstance(node, ast.Assign)
    and any(
        isinstance(target, ast.Name) and target.id == "ROOT_ARTIFACT_WRITE_SCRIPT"
        for target in node.targets
    )
]
if len(scripts) != 1:
    raise SystemExit("compatibility overlay did not define exactly one root writer")
print(scripts[0], end="")
PY
)

stream_artifact() {
  local source=$1 target=$2
  cat "$source" | docker run --rm --interactive --network none \
    --mount "type=bind,source=$logs_root,target=/logs" \
    --entrypoint /bin/bash "$image" -c "$writer_script" maka-artifact-writer "$target"
}

stream_artifact "$payload_root/stdout" /logs/artifacts/maka-subject.stdout.txt
stream_artifact "$payload_root/stderr" /logs/artifacts/maka-subject.stderr.txt
cmp -s "$payload_root/stdout" "$logs_root/artifacts/maka-subject.stdout.txt"
cmp -s "$payload_root/stderr" "$logs_root/artifacts/maka-subject.stderr.txt"
test "$(stat -f '%Lp' "$logs_root/artifacts/maka-subject.stdout.txt")" = 600
test "$(stat -f '%Lp' "$logs_root/artifacts/maka-subject.stderr.txt")" = 600
test -z "$(find "$logs_root/artifacts" -maxdepth 1 -name '.maka-subject-output.*' -print -quit)"
test -z "$(find "$logs_root/agent" -maxdepth 1 -name '.maka-subject-output.*' -print -quit)"
