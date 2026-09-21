#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

before_default=$(colima list -j | jq -sc '[.[] | select(.name == "default")][0]')
before_context=$(docker context show)
migration_in_progress=false
migration_was_running=false
migration_backup=""
profile_config="$HOME/.colima/maka-amd64/colima.yaml"
legacy_lab_root="$HOME/git/maka-lab"

restore_legacy_mount_after_failure() {
  test "$migration_in_progress" = true || return 0
  colima stop --profile maka-amd64 >/dev/null 2>&1 || true
  if test -f "$migration_backup" && test ! -L "$migration_backup"; then
    install -m 0600 "$migration_backup" "$profile_config"
  fi
  if test "$migration_was_running" = true; then
    colima start --profile maka-amd64 --activate=false >/dev/null 2>&1 || true
  fi
}

guard_default_profile_and_context() {
  local after_default after_context
  after_default=$(colima list -j | jq -sc '[.[] | select(.name == "default")][0]')
  after_context=$(docker context show)
  test "$before_default" = "$after_default" || { echo 'Default Colima profile changed unexpectedly' >&2; return 70; }
  test "$before_context" = "$after_context" || { echo 'Active Docker context changed unexpectedly' >&2; return 70; }
}
finish_guard() {
  status=$?
  trap - EXIT
  if test "$status" -ne 0; then
    restore_legacy_mount_after_failure
  fi
  guard_default_profile_and_context || status=70
  test -z "$migration_backup" || rm -f "$migration_backup"
  exit "$status"
}
trap finish_guard EXIT
profiles=$(colima list -j | jq -sc '.')
existing=$(jq -r '[.[] | select(.name == "maka-amd64")] | length' <<<"$profiles")
if test "$existing" = 0; then
  colima start --profile maka-amd64 --arch x86_64 --cpus 4 --memory 8 --disk 60 \
    --runtime docker --mount "$LAB_ROOT:w" --activate=false
else
  arch=$(jq -r '.[] | select(.name == "maka-amd64") | .arch' <<<"$profiles")
  test "$arch" = x86_64 || { echo "Existing maka-amd64 profile has unexpected architecture: $arch" >&2; exit 78; }
  status=$(jq -r '.[] | select(.name == "maka-amd64") | .status' <<<"$profiles")
  mount_state=$(ruby -ryaml -e '
    mounts = YAML.safe_load(File.read(ARGV.fetch(0))).fetch("mounts")
    current = [{"location" => ARGV.fetch(1), "writable" => true}]
    legacy = [{"location" => ARGV.fetch(2), "writable" => true}]
    if mounts == current
      puts "current"
    elsif mounts == legacy
      puts "legacy"
    else
      abort "maka-amd64 must mount only the Maka lab writable"
    end
  ' "$profile_config" "$LAB_ROOT" "$legacy_lab_root")
  if test "$mount_state" = legacy; then
    test -f "$profile_config" && test ! -L "$profile_config"
    test "$(stat -f '%u' "$profile_config")" = "$(id -u)"
    migration_backup=$(mktemp "$HOME/.colima/maka-amd64/.colima.yaml.khenrix-utils-XXXXXX")
    cp -p "$profile_config" "$migration_backup"
    chmod 0600 "$migration_backup"
    persistent_backup="$profile_config.khenrix-utils-before-relocation"
    if test ! -e "$persistent_backup"; then
      install -m 0600 "$profile_config" "$persistent_backup"
    fi
    test -f "$persistent_backup" && test ! -L "$persistent_backup"
    test "$(stat -f '%u' "$persistent_backup")" = "$(id -u)"
    migration_in_progress=true
    test "$status" = Running && migration_was_running=true
    if test "$migration_was_running" = true; then
      colima stop --profile maka-amd64
    fi
    ruby -ryaml -e '
      require "tempfile"
      path, old_root, new_root = ARGV
      metadata = File.lstat(path)
      abort "maka-amd64 config must be a regular owned file" unless metadata.file? && metadata.uid == Process.uid
      document = YAML.safe_load(File.read(path))
      expected = [{"location" => old_root, "writable" => true}]
      abort "maka-amd64 legacy mount changed during migration" unless document.fetch("mounts") == expected
      source = File.read(path)
      old_line = /^([[:space:]]*-[[:space:]]+location:[[:space:]]*)#{Regexp.escape(old_root)}[[:space:]]*$/
      abort "maka-amd64 legacy mount line is ambiguous" unless source.scan(old_line).length == 1
      updated = source.sub(old_line) { "#{$1}#{new_root}" }
      Tempfile.create([".colima.yaml.khenrix-utils-", ".tmp"], File.dirname(path)) do |temporary|
        temporary.chmod(0600)
        temporary.write(updated)
        temporary.flush
        temporary.fsync
        File.rename(temporary.path, path)
      end
    ' "$profile_config" "$legacy_lab_root" "$LAB_ROOT"
    colima start --profile maka-amd64 --activate=false
    profiles=$(colima list -j | jq -sc '.')
    status=Running
  fi
  test "$status" = Running || colima start --profile maka-amd64 --activate=false
fi

current=$(colima list -j | jq -sc '[.[] | select(.name == "maka-amd64")][0]')
test "$(jq -r .status <<<"$current")" = Running
test "$(jq -r .arch <<<"$current")" = x86_64
test "$(jq -r .runtime <<<"$current")" = docker
test "$(jq -r .cpus <<<"$current")" -ge 4
test "$(jq -r .memory <<<"$current")" -ge 8589934592
test "$(jq -r .disk <<<"$current")" -ge 64424509440
DOCKER_HOST=$(docker_host_for_lab) docker version --format '{{.Server.Arch}} {{.Server.Version}}' | grep -E '^x86_64 |^amd64 '

ruby -ryaml -e '
  mounts = YAML.safe_load(File.read(ARGV.fetch(0))).fetch("mounts")
  expected = [{"location" => ARGV.fetch(1), "writable" => true}]
  abort "maka-amd64 must mount only the Maka lab writable" unless mounts == expected
' "$profile_config" "$LAB_ROOT"

guest_mounts=$(colima ssh --profile maka-amd64 -- mount)
host_mount_targets=$(awk '$1 ~ /^lima-/ { print $3 }' <<<"$guest_mounts")
test "$host_mount_targets" = "$LAB_ROOT" || {
  echo "maka-amd64 has unexpected host mounts: ${host_mount_targets:-none}" >&2
  exit 70
}
grep -F " on $LAB_ROOT type " <<<"$guest_mounts" | grep -F '(rw,' >/dev/null
if grep -F " on $HOME type " <<<"$guest_mounts" >/dev/null; then
  echo 'maka-amd64 unexpectedly exposes the full home directory' >&2
  exit 70
fi

if test "$migration_in_progress" = true; then
  migration_in_progress=false
  rm -f "$migration_backup"
  migration_backup=""
fi

dropin='[Service]
LimitCORE=0'
for service in docker.service containerd.service; do
  colima ssh --profile maka-amd64 -- sudo -n install -d -m 0755 "/etc/systemd/system/$service.d"
  current_dropin=$(colima ssh --profile maka-amd64 -- sudo -n cat "/etc/systemd/system/$service.d/10-maka-no-core.conf" 2>/dev/null || true)
  if test "$current_dropin" != "$dropin"; then
    printf '%s\n' "$dropin" \
      | colima ssh --profile maka-amd64 -- sudo -n tee "/etc/systemd/system/$service.d/10-maka-no-core.conf" >/dev/null
    restart_daemons=true
  fi
done
if test "${restart_daemons:-false}" = true \
  || ! assert_maka_daemon_core_limits >/dev/null 2>&1; then
  colima ssh --profile maka-amd64 -- sudo -n systemctl daemon-reload
  colima ssh --profile maka-amd64 -- sudo -n systemctl restart containerd.service
  colima ssh --profile maka-amd64 -- sudo -n systemctl restart docker.service
fi
assert_maka_daemon_core_limits
guard_default_profile_and_context
trap - EXIT
