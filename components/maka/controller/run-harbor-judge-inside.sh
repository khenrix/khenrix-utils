#!/usr/bin/env bash
set -euo pipefail
umask 077
ulimit -S -c 0
ulimit -H -c 0

case "${OPENAI_API_KEY:-}" in
  maka-decoy-*) ;;
  *) echo 'Harbor judge did not receive a broker decoy' >&2; exit 78 ;;
esac
test -n "${MAKA_JUDGE_REQUEST_FILE:-}" || exit 64
test -n "${MAKA_JUDGE_EXPECTED_BOUNDARY:-}" || exit 64
test -n "${MAKA_EVAL_SECRET_VOLUME:-}" || exit 64
test -n "${MAKA_EVIDENCE_DIR:-}" || exit 64
test -n "${MAKA_EVAL_RUNTIME_ROOT:-}" || exit 64
test -n "${MAKA_INSTALL_RECEIPT:-}" || exit 64
test -n "${MAKA_CONTROLLER_IMAGE_ID:-}" || exit 64
test -n "${MAKA_EGRESS_IMAGE_ID:-}" || exit 64
test "$MAKA_LAB_ROOT" = "$PWD"
test -p "$MAKA_JUDGE_REQUEST_FILE"
test -f "$MAKA_INSTALL_RECEIPT"
test "$MAKA_EVAL_RUNTIME_ROOT/maka-agent" = "$MAKA_EVAL_MAKA_BUNDLE_PATH"
test "$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{.Id}}')" = "$MAKA_EGRESS_IMAGE_ID"
test "$(docker image inspect maka-lab-controller:0.2.0-dev.62.20260928 --format '{{.Id}}')" = "$MAKA_CONTROLLER_IMAGE_ID"

compose="$MAKA_LAB_ROOT/controller/egress-overlay/docker-compose-harbor-judge.yaml"
project="maka-judge-$(date -u +%Y%m%d%H%M%S)-$$-$RANDOM"
case "$project" in
  maka-judge-[0-9]*-[0-9]*-[0-9]*) ;;
  *) echo 'Harbor judge project identity is invalid' >&2; exit 78 ;;
esac
export MAKA_JUDGE_DECOY="$OPENAI_API_KEY"
unset OPENAI_API_KEY
result_file="$MAKA_EVIDENCE_DIR/harbor-judge-result.json"
envelope_file="$MAKA_EVIDENCE_DIR/harbor-judge-envelope.json"
judge_evidence_file="$MAKA_EVIDENCE_DIR/harbor-judge-evidence.json"

finish() {
  status=$?
  trap - EXIT INT TERM
  if ! docker compose --project-name "$project" -f "$compose" down -v --remove-orphans >/dev/null 2>&1; then
    status=70
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose --project-name "$project" -f "$compose" config --quiet
set +e
"$MAKA_EVAL_HARBOR_PYTHON" -c '
import os
import signal
import stat
import sys

def expire(_signal, _frame):
    raise TimeoutError

signal.signal(signal.SIGALRM, expire)
try:
    signal.alarm(180)
    request_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
except (OSError, TimeoutError):
    print("Harbor judge request FIFO writer unavailable", file=sys.stderr)
    raise SystemExit(78)
finally:
    signal.alarm(0)
if not stat.S_ISFIFO(os.fstat(request_fd).st_mode):
    print("Harbor judge request FIFO changed", file=sys.stderr)
    raise SystemExit(78)
if request_fd != 0:
    os.dup2(request_fd, 0)
    os.close(request_fd)
os.set_inheritable(0, True)
os.execvp(sys.argv[2], sys.argv[2:])
' "$MAKA_JUDGE_REQUEST_FILE" docker compose --project-name "$project" -f "$compose" \
  run --rm -T judge > "$envelope_file"
judge_status=$?
set -e
if test "$judge_status" -ne 0; then
  if ! /usr/bin/timeout -k 2 10 docker compose --project-name "$project" -f "$compose" \
      exec -T maka-eval-mitmproxy python - \
      < "$MAKA_LAB_ROOT/scripts/harbor_judge_audit.py" >&2 2>/dev/null; then
    echo 'maka harbor judge: proxy audit unavailable' >&2
  fi
  exit "$judge_status"
fi
expected_boundary=$MAKA_JUDGE_EXPECTED_BOUNDARY
jq -e --arg boundary "$expected_boundary" '
  (keys | sort) == ["evidence", "result"]
  and (.result | keys | sort) == (["auth_route", "effort", "observed_model", "requested_model", "tool_boundary_sha256", "tool_calls", "winner"] | sort)
  and .result.auth_route == "harbor-lab"
  and .result.requested_model == "gpt-6.1-sol"
  and .result.observed_model == "gpt-6.1-sol"
  and .result.effort == "xhigh"
  and .result.tool_calls == 0
  and .result.tool_boundary_sha256 == $boundary
  and (.result.winner == "A" or .result.winner == "B" or .result.winner == "tie" or .result.winner == "abstain")
  and (.evidence | keys | sort) == ["relay_receipt", "schema", "transcript_sha256"]
  and .evidence.schema == "khenrix-maka-harbor-judge-evidence-v1"
  and (.evidence.transcript_sha256 | test("^[0-9a-f]{64}$"))
  and (.evidence.relay_receipt | keys | sort) == (["schema", "time_utc_ms", "request_marker_sha256", "prompt_sha256", "requested_model", "observed_model", "requested_service_tier", "observed_service_tier", "outbound_effort", "tool_count", "http_status", "relay_source_sha256"] | sort)
  and .evidence.relay_receipt.schema == "khenrix-maka-harbor-judge-request-v1"
  and .evidence.relay_receipt.requested_model == "gpt-6.1-sol"
  and .evidence.relay_receipt.observed_model == "gpt-6.1-sol"
  and .evidence.relay_receipt.requested_service_tier == "default"
  and .evidence.relay_receipt.observed_service_tier == "default"
  and .evidence.relay_receipt.http_status == 200
  and .evidence.relay_receipt.outbound_effort == "xhigh"
  and .evidence.relay_receipt.tool_count == 0
  and (.evidence.relay_receipt.request_marker_sha256 | test("^[0-9a-f]{64}$"))
  and (.evidence.relay_receipt.prompt_sha256 | test("^[0-9a-f]{64}$"))
' "$envelope_file" >/dev/null
jq -c .result "$envelope_file" > "$result_file"
jq -c .evidence "$envelope_file" > "$judge_evidence_file"
rm -f "$envelope_file"
cat "$result_file"
