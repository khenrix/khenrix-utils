#!/usr/bin/env bash
set -euo pipefail
umask 077
ulimit -S -c 0
ulimit -H -c 0

boundary_only=false
if test "$#" -eq 1 && test "$1" = --boundary; then
  boundary_only=true
elif test "$#" -eq 2 && test "$1" = --live && test "$2" = -; then
  if test "${MAKA_JUDGE_PAID_RUN:-}" != 1; then
    echo 'Harbor judge requires MAKA_JUDGE_PAID_RUN=1 for a paid run' >&2
    exit 78
  fi
  unset MAKA_JUDGE_PAID_RUN
else
  echo 'Harbor judge requires an explicit live --live - invocation' >&2
  exit 64
fi
source "$(dirname "$0")/lib.sh"

expected_root="$HOME/.local/share/khenrix-utils/maka"
if test "$LAB_ROOT" != "$expected_root" || test -L "$LAB_ROOT" \
  || test "$(stat -f '%u' "$LAB_ROOT")" != "$(id -u)" \
  || test "$(stat -f '%Lp' "$LAB_ROOT")" != 700; then
  echo 'Harbor judge requires the private installed Maka component' >&2
  exit 78
fi

python "$LAB_ROOT/scripts/component_doctor.py" >/dev/null
runtime_root="$LAB_ROOT/runtime/linux-amd64/maka-0.2.0-dev.62.20260928-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v1"
install_receipt="$HOME/.local/state/khenrix-utils/maka/install-receipt.json"
profile=$(colima list -j | jq -sc '[.[] | select(.name == "maka-amd64")][0]')
test "$(jq -r .status <<< "$profile")" = Running
test "$(jq -r .arch <<< "$profile")" = x86_64
assert_maka_daemon_core_limits >/dev/null
assert_maka_guest_source_parity
export DOCKER_HOST
DOCKER_HOST=$(docker_host_for_lab)

test -f "$runtime_root/RUNTIME_SHA256SUMS"
test -f "$runtime_root/RUNTIME_SYMLINKS"
controller_image_id=$(docker image inspect maka-lab-controller:0.2.0-dev.62.20260928 --format '{{.Id}}')
egress_image_id=$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{.Id}}')
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = 2f32205528a4436783c800715261d4b6e286023d
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "io.maka.judge.compatibility-overlay"}}')" = relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v1
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "io.maka.judge.runtime-variant"}}')" = harbor-judge-sol61-h2-v1
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "io.maka.judge.model-fetcher-sha256"}}')" = 7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "io.maka.judge.model-metadata-sha256"}}')" = 9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "io.maka.judge.undici-h2-base-sha256"}}')" = 611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7
test "$(docker image inspect "$controller_image_id" --format '{{index .Config.Labels "io.maka.judge.undici-h2-sha256"}}')" = 521fef59ccb84909a3c68d52c15e972f61836e0d386093d5d4f4e64ba7d85087
test "$(docker image inspect "$egress_image_id" --format '{{index .Config.Labels "io.maka.lab.source-revision"}}')" = 2f32205528a4436783c800715261d4b6e286023d
expected_filter=$(shasum -a 256 "$LAB_ROOT/controller/egress-overlay/egress_filter.py" | awk '{print $1}')
installed_filter=$(docker run --rm --network none --entrypoint sha256sum "$egress_image_id" \
  /opt/maka-eval/egress_filter.py | awk '{print $1}')
test "$expected_filter" = "$installed_filter"
if test "$boundary_only" = true; then
  exec node "$LAB_ROOT/controller/harbor_judge.mjs" --boundary \
    --runtime-root "$runtime_root" --install-receipt "$install_receipt" \
    --controller-image-id "$controller_image_id" --egress-image-id "$egress_image_id"
fi

assert_maka_guest_provider_dns

install -d -m 0700 "$LAB_ROOT/controller/.build"
request_b64=$(python "$LAB_ROOT/scripts/harbor_judge_input.py" capture)
request_hash=$(printf '%s' "$request_b64" | python "$LAB_ROOT/scripts/harbor_judge_input.py" digest)
request_boundary=$(printf '%s' "$request_b64" | python "$LAB_ROOT/scripts/harbor_judge_input.py" boundary)
request_dir=$(mktemp -d "$LAB_ROOT/controller/.build/harbor-judge-XXXXXX")
request_fifo="$request_dir/input.fifo"
probe_fifo="$request_dir/probe.fifo"
staged_receipt="$request_dir/install-receipt.json"
writer_pid=
probe_writer_pid=
secret_volume="maka-judge-secret-$(date -u +%Y%m%d%H%M%S)-$$-$RANDOM"
secret_volume_created=false
evidence_dir=
finish() {
  status=$?
  trap - EXIT INT TERM
  if test -n "$probe_writer_pid"; then
    kill "$probe_writer_pid" 2>/dev/null || true
    wait "$probe_writer_pid" 2>/dev/null || true
  fi
  if test -n "$writer_pid"; then
    kill "$writer_pid" 2>/dev/null || true
    wait "$writer_pid" 2>/dev/null || true
  fi
  if test "$secret_volume_created" = true; then
    cleanup_secret_volume "$secret_volume" || status=70
  fi
  rm -f "$request_fifo" "$probe_fifo" "$staged_receipt" || status=70
  rmdir "$request_dir" || status=70
  if test -n "$evidence_dir" && test -d "$evidence_dir"; then
    finalize_evidence "$evidence_dir" || status=70
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkfifo -m 0600 "$request_fifo"
stage_maka_install_receipt "$install_receipt" "$staged_receipt"
mkfifo -m 0600 "$probe_fifo"
maka_guest_fifo_write probe-write "$probe_fifo" &
probe_writer_pid=$!
if ! probe_result=$(colima ssh --profile maka-amd64 -- \
    python3 "$LAB_ROOT/scripts/harbor_judge_input.py" probe-read "$probe_fifo" \
    </dev/null 2>/dev/null); then
  echo 'Maka guest FIFO transport is unavailable' >&2
  exit 78
fi
if ! wait "$probe_writer_pid" || test "$probe_result" != maka-fifo-probe-v1; then
  echo 'Maka guest FIFO transport is unavailable' >&2
  exit 78
fi
probe_writer_pid=
rm -f "$probe_fifo"

evidence_dir=$(new_evidence_dir harbor-judge)
chmod 0700 "$evidence_dir"
printf '%s\n' "$request_hash" > "$evidence_dir/request-SHA256SUM"
shasum -a 256 "$LAB_ROOT/controller/harbor_judge.mjs" \
  "$LAB_ROOT/controller/egress-overlay/egress_filter.py" \
  "$LAB_ROOT/controller/egress-overlay/docker-compose-harbor-judge.yaml" \
  > "$evidence_dir/source-SHA256SUMS"
printf '%s\n%s\n' "$controller_image_id" "$egress_image_id" \
  > "$evidence_dir/image-ids.txt"

docker volume create --label io.maka.lab.secret-broker=true \
  --label "io.maka.lab.run=$secret_volume" "$secret_volume" >/dev/null
secret_volume_created=true
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  /usr/bin/python3 -I -S -B "$LAB_ROOT/scripts/harden_python_runtime.py" verify >/dev/null

printf '%s' "$request_b64" | maka_guest_fifo_write deliver "$request_fifo" &
writer_pid=$!
unset request_b64
set +e
result_payload=$(/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  /usr/bin/python3 -I -S -B "$LAB_ROOT/scripts/run_hardened_python.py" key-export \
  | docker run --rm -i --ulimit core=0 \
      --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
      --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT" \
      --mount "type=volume,source=$secret_volume,target=/run/maka-secret" \
      --workdir "$LAB_ROOT" \
      --env "MAKA_LAB_ROOT=$LAB_ROOT" \
      --env MAKA_EVAL_RUNTIME_VARIANT=harbor-judge-sol61-h2-v1 \
      --env "MAKA_JUDGE_REQUEST_FILE=$request_fifo" \
      --env "MAKA_JUDGE_EXPECTED_BOUNDARY=$request_boundary" \
      --env "MAKA_EVIDENCE_DIR=$evidence_dir" \
      --env "MAKA_EVAL_RUNTIME_ROOT=$runtime_root" \
      --env "MAKA_INSTALL_RECEIPT=$staged_receipt" \
      --env "MAKA_EVAL_MAKA_BUNDLE_PATH=$runtime_root/maka-agent" \
      --env "MAKA_CONTROLLER_IMAGE_ID=$controller_image_id" \
      --env "MAKA_EGRESS_IMAGE_ID=$egress_image_id" \
      --env "MAKA_EVAL_SECRET_VOLUME=$secret_volume" \
      "$controller_image_id" \
      python controller/secret-broker-exec.py bash controller/run-harbor-judge-inside.sh)
run_status=$?
set -e
if test "$run_status" -ne 0; then
  kill "$writer_pid" 2>/dev/null || true
  wait "$writer_pid" 2>/dev/null || true
  writer_pid=
  exit "$run_status"
fi
wait "$writer_pid"
writer_pid=
test "$run_status" -eq 0
printf '%s\n' "$result_payload" | jq -e '.auth_route == "harbor-lab" and .tool_calls == 0' >/dev/null
printf '%s\n' "$result_payload"
