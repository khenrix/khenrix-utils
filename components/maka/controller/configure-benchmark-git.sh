#!/usr/bin/env bash

# Stage the pinned benchmark from one small, host-created Git bundle. Git's
# metadata walk can block indefinitely on the macOS/Colima 9p bind mount, so
# every Git check and the Harbor evaluation use this native controller copy.

script_lab_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
test "${MAKA_LAB_ROOT:-}" = "$script_lab_root" || {
  echo 'Refusing an unexpected Maka lab root' >&2
  return 64 2>/dev/null || exit 64
}
test "${HOME:-}" = /root || {
  echo 'Refusing to stage the benchmark outside the disposable controller home' >&2
  return 64 2>/dev/null || exit 64
}

benchmark_bundle=${MAKA_BENCHMARK_BUNDLE_PATH:-}
benchmark_bundle_sha256=${MAKA_BENCHMARK_BUNDLE_SHA256:-}
expected_bundle_parent="$MAKA_LAB_ROOT/controller/.build"
case "$benchmark_bundle" in
  "$expected_bundle_parent"/benchmark-bundle-[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]/fix-git.bundle) ;;
  *) echo 'Refusing an unexpected benchmark bundle path' >&2; return 64 2>/dev/null || exit 64 ;;
esac
test "$(realpath -e "$benchmark_bundle")" = "$benchmark_bundle" || {
  echo 'Benchmark bundle must be a canonical existing path' >&2
  return 64 2>/dev/null || exit 64
}
test -f "$benchmark_bundle" && test ! -L "$benchmark_bundle" || {
  echo 'Benchmark bundle must be a regular non-symlink file' >&2
  return 64 2>/dev/null || exit 64
}
test "$(stat -c %h "$benchmark_bundle")" = 1 || {
  echo 'Benchmark bundle must not be hard-linked' >&2
  return 64 2>/dev/null || exit 64
}
bundle_size=$(stat -c %s "$benchmark_bundle")
test "$bundle_size" -gt 0 && test "$bundle_size" -le 16777216 || {
  echo 'Benchmark bundle is outside the allowed size bound' >&2
  return 64 2>/dev/null || exit 64
}
test "${#benchmark_bundle_sha256}" -eq 64 || {
  echo 'Benchmark bundle SHA-256 is malformed' >&2
  return 64 2>/dev/null || exit 64
}
case "$benchmark_bundle_sha256" in
  *[!0-9a-f]*) echo 'Benchmark bundle SHA-256 is malformed' >&2; return 64 2>/dev/null || exit 64 ;;
esac
test "$(sha256sum "$benchmark_bundle" | cut -d ' ' -f1)" = "$benchmark_bundle_sha256" || {
  echo 'Mounted benchmark bundle failed SHA-256 verification' >&2
  return 70 2>/dev/null || exit 70
}

benchmark_native_root=$(mktemp -d /tmp/maka-benchmark-XXXXXX)
chmod 0700 "$benchmark_native_root"
native_bundle="$benchmark_native_root/fix-git.bundle"
install -m 0600 "$benchmark_bundle" "$native_bundle"
test "$(sha256sum "$native_bundle" | cut -d ' ' -f1)" = "$benchmark_bundle_sha256" || {
  echo 'Native benchmark bundle failed SHA-256 verification' >&2
  return 70 2>/dev/null || exit 70
}

expected_commit=cd855d42cdea03da2d781ae1dbc89f74b39a4491
expected_tree=43a3b7807bc2dc02c200f12b3575ae966e511c19
expected_task_tree=08b47ce8eebc1d6e43b814bba691d6d559d7c5de
test "$(git bundle list-heads "$native_bundle" | LC_ALL=C sort)" \
  = "$(printf '%s\n%s' "$expected_commit HEAD" "$expected_commit refs/heads/main")" || {
  echo 'Benchmark bundle does not contain exactly the pinned HEAD and main ref' >&2
  return 70 2>/dev/null || exit 70
}

benchmark_repository="$benchmark_native_root/fix-git-repo"
git_clean=(
  env -i HOME=/root PATH="$PATH"
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
  git -c init.defaultBranch=main -c core.fsmonitor=false -c core.hooksPath=/dev/null
)
"${git_clean[@]}" clone --quiet --no-local --no-hardlinks \
  "$native_bundle" "$benchmark_repository"
test "$("${git_clean[@]}" -C "$benchmark_repository" symbolic-ref HEAD)" = refs/heads/main
test "$("${git_clean[@]}" -C "$benchmark_repository" rev-parse HEAD)" = "$expected_commit"
test "$("${git_clean[@]}" -C "$benchmark_repository" rev-parse 'HEAD^{tree}')" = "$expected_tree"
test "$("${git_clean[@]}" -C "$benchmark_repository" write-tree)" = "$expected_tree"
test "$("${git_clean[@]}" -C "$benchmark_repository" rev-parse HEAD:tasks/fix-git)" = "$expected_task_tree"
test -z "$("${git_clean[@]}" -C "$benchmark_repository" status --porcelain=v1 --untracked-files=all)"
"${git_clean[@]}" -C "$benchmark_repository" fsck --full --strict
test "$(sed -n 's/^docker_image = "\(.*\)"/\1/p' "$benchmark_repository/tasks/fix-git/task.toml")" \
  = alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74

benchmark_runtime_config="$benchmark_native_root/harbor-fix-git.json"
jq --arg repository "file://$benchmark_repository" \
  '.benchmark.config.repository = $repository' \
  "$MAKA_LAB_ROOT/config/harbor-fix-git.json" > "$benchmark_runtime_config"
chmod 0600 "$benchmark_runtime_config"
test "$(jq -r '.benchmark.config.repository' "$benchmark_runtime_config")" = "file://$benchmark_repository"
test "$(jq -S '.benchmark.config.repository = "<native-benchmark-repository>"' "$MAKA_LAB_ROOT/config/harbor-fix-git.json" | sha256sum | cut -d ' ' -f1)" \
  = "$(jq -S '.benchmark.config.repository = "<native-benchmark-repository>"' "$benchmark_runtime_config" | sha256sum | cut -d ' ' -f1)"
