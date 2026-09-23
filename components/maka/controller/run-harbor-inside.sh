#!/usr/bin/env bash
set -euo pipefail
umask 077

case "${OPENAI_API_KEY:-}" in
  maka-decoy-*) ;;
  *) echo 'Eval child did not receive a broker decoy credential' >&2; exit 65 ;;
esac
test -n "${MAKA_EVAL_SECRET_VOLUME:-}" || { echo 'MAKA_EVAL_SECRET_VOLUME is required' >&2; exit 64; }
test -n "${MAKA_EVIDENCE_DIR:-}" || { echo 'MAKA_EVIDENCE_DIR is required' >&2; exit 64; }
test -n "${MAKA_EVAL_RUNTIME_ROOT:-}" || { echo 'MAKA_EVAL_RUNTIME_ROOT is required' >&2; exit 64; }
benchmark_native_root=
cleanup_native_benchmark() {
  status=$?
  trap - EXIT INT TERM
  if test -n "$benchmark_native_root"; then
    case "$benchmark_native_root" in
      /tmp/maka-benchmark-[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9])
        chmod -R u+w "$benchmark_native_root" 2>/dev/null || status=70
        find "$benchmark_native_root" -depth -delete 2>/dev/null || status=70
        ;;
      *) status=70 ;;
    esac
  fi
  exit "$status"
}
trap cleanup_native_benchmark EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
source controller/configure-benchmark-git.sh
cp "$MAKA_EVAL_RUNTIME_ROOT/RUNTIME_SHA256SUMS" "$MAKA_EVIDENCE_DIR/runtime-file-SHA256SUMS"
cp "$MAKA_EVAL_RUNTIME_ROOT/RUNTIME_SYMLINKS" "$MAKA_EVIDENCE_DIR/runtime-symlinks.txt"
printf '%s  %s\n' "$benchmark_bundle_sha256" fix-git.bundle \
  > "$MAKA_EVIDENCE_DIR/benchmark-bundle-SHA256SUM"
jq '.benchmark.config.repository = "<native-benchmark-repository>"' \
  "$benchmark_runtime_config" > "$MAKA_EVIDENCE_DIR/harbor-runtime-config.normalized.json"
/opt/venvs/harbor-0.20.0/bin/python controller/assert-secret-mounts.py >/dev/null
test "$(jq -r '.benchmark.config.repository' config/harbor-fix-git.json)" \
  = "file:///invalid/replaced-by-the-maka-controller"
test "$(jq -r '.subjects[0].config.model' "$benchmark_runtime_config")" = gpt-5.6-sol
test "$(jq -r '.subjects[0].config.thinkingLevel' "$benchmark_runtime_config")" = xhigh
test "$(jq -r '.execution.maxConcurrentTaskGroups' "$benchmark_runtime_config")" = 1
test "$(jq -r '.repetitions' "$benchmark_runtime_config")" = 1
test "$(jq -r '.tasks | length' "$benchmark_runtime_config")" = 1
test "$(jq -r '.budget.maxSteps' "$benchmark_runtime_config")" = 32
test "$(jq -r '.benchmark.version' "$benchmark_runtime_config")" = cd855d42cdea03da2d781ae1dbc89f74b39a4491
test "$(jq -r '.benchmark.config.repository' "$benchmark_runtime_config")" = "file://$benchmark_repository"

mkdir -p "$MAKA_EVIDENCE_DIR/eval" "$MAKA_EVIDENCE_DIR/trials"
export MAKA_EVAL_TRIALS_DIR="$MAKA_EVIDENCE_DIR/trials"

test "$MAKA_EVAL_MAKA_BUNDLE_PATH" = "$MAKA_EVAL_RUNTIME_ROOT/maka-agent"
test "$MAKA_EVAL_NODE_TOOLCHAIN_PATH" = "$MAKA_EVAL_RUNTIME_ROOT/node"
maka_runtime_node="$MAKA_EVAL_NODE_TOOLCHAIN_PATH/bin/node"
maka_runtime_cli="$MAKA_EVAL_MAKA_BUNDLE_PATH/dist/cli.js"
test -x "$maka_runtime_node"
test -f "$maka_runtime_cli"
# Node resolves @maka/eval from the launched CLI. The bundle environment variable
# identifies the payload but does not redirect module resolution from mise's CLI.
"$maka_runtime_node" controller/assert-eval-runtime-path.mjs \
  "$MAKA_EVAL_RUNTIME_ROOT" "$MAKA_EVAL_MAKA_BUNDLE_PATH" \
  > "$MAKA_EVIDENCE_DIR/maka-eval-runtime-path.json"

test -n "${MAKA_EGRESS_IMAGE_ID:-}" || { echo 'MAKA_EGRESS_IMAGE_ID is required' >&2; exit 64; }
test "$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{.Id}}')" = "$MAKA_EGRESS_IMAGE_ID"
test "$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{index .Config.Labels "io.maka.lab.source-revision"}}')" = 6cb8c58084d043f9b87421807fbee1d1ad3bdc03
docker image inspect alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74 > "$MAKA_EVIDENCE_DIR/task-image.json"

set +e
"$maka_runtime_node" "$maka_runtime_cli" eval run \
  "$benchmark_runtime_config" --out "$MAKA_EVIDENCE_DIR/eval" \
  > "$MAKA_EVIDENCE_DIR/eval.stdout" 2> "$MAKA_EVIDENCE_DIR/eval.stderr"
eval_status=$?
set -e

mapfile -d '' attempt_files < <(find "$MAKA_EVIDENCE_DIR/eval/attempts" -type f -name '*.json' -print0 2>/dev/null || true)
result_status=$eval_status
if test "${#attempt_files[@]}" -eq 1; then
  jq '{cellId,sequence,startedAt,completedAt,result:{status:.result.status,score:.result.score,failureReason:.result.failureReason,costUsd:.result.costUsd,usage:.result.usage}}' \
    "${attempt_files[0]}" > "$MAKA_EVIDENCE_DIR/benchmark-outcome.json"
  if ! jq -e '.result.status == "completed" and .result.score == 1' "${attempt_files[0]}" >/dev/null; then
    result_status=1
  fi
else
  printf '{"attemptFiles":%d,"valid":false}\n' "${#attempt_files[@]}" > "$MAKA_EVIDENCE_DIR/benchmark-outcome.json"
  result_status=1
fi
mapfile -d '' egress_audit_files < <(find "$MAKA_EVIDENCE_DIR/trials" -type f -name 'egress-hits.jsonl' -print0 2>/dev/null || true)
authorized_count=0
audit_valid=false
if test "${#egress_audit_files[@]}" -eq 1; then
  authorized_count=$(jq -s '[.[] | select(.ruleId == "openai_authorized")] | length' "${egress_audit_files[0]}")
  if ! jq -se '
    . as $records
    | [$records[1:][] | select(.ruleId == "openai_authorized")
        | (.normalizedPath | capture("^sequence=(?<n>[0-9]+)$").n | tonumber)] as $sequence
    | ($records | length) == (($sequence | length) + 1)
      and $records[0].ruleId == "openai_websocket_disabled"
      and $records[0].normalizedPath == "sequence=1"
      and all($records[1:][]; .ruleId == "openai_authorized")
      and ($sequence | length) >= 1
      and ($sequence | length) <= 32
      and $sequence == [range(1; ($sequence | length) + 1)]
  ' "${egress_audit_files[0]}" >/dev/null; then
    result_status=1
  else
    audit_valid=true
  fi
else
  result_status=1
fi
printf '{"auditFiles":%d,"expectedWebSocketFallbacks":1,"authorizedOpenAIRequests":%d,"limit":32,"valid":%s}\n' \
  "${#egress_audit_files[@]}" "$authorized_count" \
  "$audit_valid" \
  > "$MAKA_EVIDENCE_DIR/openai-request-count.json"
unset OPENAI_API_KEY
printf '{"evalExitCode":%d,"resultExitCode":%d,"model":"gpt-5.6-sol","thinking":"xhigh","task":"fix-git","repetitions":1,"maxConcurrentTaskGroups":1,"maxSteps":32}\n' \
  "$eval_status" "$result_status" > "$MAKA_EVIDENCE_DIR/run.json"
exit "$result_status"
