#!/usr/bin/env bash
set -euo pipefail

# The released CLI auto-onboards ambient provider keys into a plaintext vault.
# Ordinary lab tasks stay keyless; only harbor-smoke retrieves one key through stdin.
unset OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY GEMINI_API_KEY GOOGLE_API_KEY \
  GOOGLE_GENERATIVE_AI_API_KEY XAI_API_KEY GROQ_API_KEY MISTRAL_API_KEY COHERE_API_KEY \
  TOGETHER_API_KEY OPENROUTER_API_KEY
while IFS='=' read -r variable_name _value; do
  case "$variable_name" in
    *_API_KEY) unset "$variable_name" ;;
  esac
done < <(env)

LAB_ROOT=${MAKA_LAB_ROOT:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}
export LAB_ROOT

new_evidence_dir() {
  local label=$1
  mkdir -p "$LAB_ROOT/evidence/runs"
  mktemp -d "$LAB_ROOT/evidence/runs/$(date -u +%Y%m%dT%H%M%SZ)-${label}-XXXXXX"
}

maka_install_root() {
  local shim install_root package_root
  shim=$(mise -C "$LAB_ROOT" which maka)
  install_root=$(cd "$(dirname "$shim")/../.." && pwd)
  package_root=$(find "$install_root" -type f -path '*/node_modules/maka-agent/package.json' -print -quit)
  test -n "$package_root"
  dirname "$package_root"
}

maka_profile_root() {
  python "$LAB_ROOT/interactive/maka_profile.py" resolve --managed
}

ensure_benchmark_repository() {
  local bundle bundle_hash parent repository staging
  local -a git_clean
  bundle="$LAB_ROOT/benchmark/fix-git.bundle"
  bundle_hash=6ef73219e91ce92cb901f04b07c419f315da7f2a823730f7b994a1fb3cadfe51
  parent="$LAB_ROOT/runtime/benchmark"
  repository="$parent/fix-git-repo"
  git_clean=(env -i HOME="$HOME" PATH="$PATH" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null)

  test -f "$bundle" && test ! -L "$bundle"
  test "$(shasum -a 256 "$bundle" | awk '{print $1}')" = "$bundle_hash"
  test "$("${git_clean[@]}" bundle list-heads "$bundle" | LC_ALL=C sort)" \
    = "$(printf '%s\n%s' \
      'cd855d42cdea03da2d781ae1dbc89f74b39a4491 HEAD' \
      'cd855d42cdea03da2d781ae1dbc89f74b39a4491 refs/heads/main')"

  mkdir -p "$parent"
  if test ! -d "$repository/.git"; then
    test ! -e "$repository"
    staging=$(mktemp -d "$parent/.fix-git-repo-XXXXXX")
    if ! "${git_clean[@]}" clone --quiet --no-local --no-hardlinks "$bundle" "$staging/repository"; then
      find "$staging" -depth -delete
      return 70
    fi
    if ! mv "$staging/repository" "$repository"; then
      find "$staging" -depth -delete
      return 70
    fi
    rmdir "$staging"
  fi

  test -d "$repository/.git" && test ! -L "$repository"
  test "$("${git_clean[@]}" -C "$repository" rev-parse HEAD)" = cd855d42cdea03da2d781ae1dbc89f74b39a4491
  test "$("${git_clean[@]}" -C "$repository" rev-parse 'HEAD^{tree}')" = 43a3b7807bc2dc02c200f12b3575ae966e511c19
  test "$("${git_clean[@]}" -C "$repository" rev-parse HEAD:tasks/fix-git)" = 08b47ce8eebc1d6e43b814bba691d6d559d7c5de
  test -z "$("${git_clean[@]}" -C "$repository" status --porcelain=v1 --untracked-files=all)"
  "${git_clean[@]}" -C "$repository" fsck --full --strict >/dev/null
  printf '%s\n' "$repository"
}

hash_evidence() {
  local directory=$1
  if find "$directory" ! -type f ! -type d -print -quit | grep -q .; then
    echo "Evidence contains a non-regular inode and cannot be sealed: $directory" >&2
    return 70
  fi
  (
    cd "$directory"
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS
    find . -type f -exec chmod 0400 {} +
    chmod 0400 SHA256SUMS
  )
  find "$directory" -depth -type d -exec chmod 0500 {} +
}

finalize_evidence() {
  local directory=$1
  test ! -d "$directory" || hash_evidence "$directory"
}

record_run_inputs() {
  local directory=$1 spec=$2 benchmark_repository benchmark_relative
  benchmark_repository=$(ensure_benchmark_repository)
  benchmark_relative=${benchmark_repository#"$LAB_ROOT"/}
  (
    cd "$LAB_ROOT"
    {
      printf '%s\n' .dockerignore .gitignore README.md provenance.json mise.toml mise.lock "$spec"
      find requirements controller config scripts fixtures -type f \
        ! -path '*/.build/*' ! -path '*/__pycache__/*' ! -name '*.pyc' -print
      printf '%s\n' benchmark/SHA256SUMS benchmark/fix-git.bundle benchmark/upstream-fix-git-tree
      git -C "$benchmark_relative" ls-files \
        | sed "s#^#$benchmark_relative/#"
    } | LC_ALL=C sort -u | while IFS= read -r path; do
      test -f "$path"
      shasum -a 256 "$path"
    done
  ) > "$directory/run-input-SHA256SUMS"
  git -C "$benchmark_repository" rev-parse HEAD > "$directory/benchmark-local-commit.txt"
  git -C "$benchmark_repository" rev-parse 'HEAD^{tree}' > "$directory/benchmark-local-tree.txt"
  git -C "$benchmark_repository" rev-parse HEAD:tasks/fix-git > "$directory/benchmark-task-tree.txt"
  git -C "$benchmark_repository" status --porcelain=v1 --untracked-files=all > "$directory/benchmark-status.txt"
  git -C "$benchmark_repository" diff --binary HEAD -- > "$directory/benchmark-worktree.diff"
  git -C "$benchmark_repository" diff --cached --binary HEAD -- > "$directory/benchmark-index.diff"
  shasum -a 256 "$HOME/.local/bin/maka" > "$directory/maka-wrapper-SHA256SUM"
}

docker_host_for_lab() {
  printf 'unix://%s/.colima/maka-amd64/docker.sock\n' "$HOME"
}

assert_maka_daemon_core_limits() {
  colima ssh --profile maka-amd64 -- sh -lc '
    set -eu
    for service in docker.service containerd.service; do
      hard=$(systemctl show "$service" --property LimitCORE --value)
      soft=$(systemctl show "$service" --property LimitCORESoft --value)
      test "$hard" = 0
      test "$soft" = 0
      test "$(cat "/etc/systemd/system/$service.d/10-maka-no-core.conf")" = "[Service]
LimitCORE=0"
      printf "%s hard=%s soft=%s\n" "$service" "$hard" "$soft"
    done
    for daemon in dockerd containerd; do
      pid=$(pgrep -xo "$daemon")
      test -n "$pid"
      set -- $(awk "/^Max core file size/ { print \$5, \$6 }" "/proc/$pid/limits")
      test "$1" = 0
      test "$2" = 0
      printf "%s pid=%s hard=%s soft=%s\n" "$daemon" "$pid" "$2" "$1"
    done
    test ! -e /core
    printf "vm-core-file=absent\n"
  '
}

# Remove every container that can access one unique broker volume, then remove
# the volume. This function performs no evidence writes so cleanup remains
# independent of a missing, full, or unwritable evidence directory.
cleanup_secret_volume() {
  local secret_volume=$1 container_id cleanup_ok
  cleanup_ok=true
  SECRET_CLEANUP_CONTAINERS_BEFORE=""
  SECRET_CLEANUP_CONTAINERS_AFTER=""
  SECRET_CLEANUP_VOLUME_REMOVED=false
  SECRET_CLEANUP_OK=false

  if ! SECRET_CLEANUP_CONTAINERS_BEFORE=$(docker container ls -aq --filter "volume=$secret_volume" 2>/dev/null); then
    cleanup_ok=false
  fi
  while IFS= read -r container_id; do
    if test -n "$container_id" && ! docker container rm -f "$container_id" >/dev/null 2>&1; then
      cleanup_ok=false
    fi
  done <<EOF
$SECRET_CLEANUP_CONTAINERS_BEFORE
EOF
  if ! SECRET_CLEANUP_CONTAINERS_AFTER=$(docker container ls -aq --filter "volume=$secret_volume" 2>/dev/null); then
    cleanup_ok=false
  fi
  test -z "$SECRET_CLEANUP_CONTAINERS_AFTER" || cleanup_ok=false
  if docker volume rm "$secret_volume" >/dev/null 2>&1; then
    SECRET_CLEANUP_VOLUME_REMOVED=true
  else
    cleanup_ok=false
  fi
  if docker volume inspect "$secret_volume" >/dev/null 2>&1; then
    SECRET_CLEANUP_VOLUME_REMOVED=false
    cleanup_ok=false
  fi
  SECRET_CLEANUP_OK=$cleanup_ok
  test "$SECRET_CLEANUP_OK" = true
}
