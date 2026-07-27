#!/usr/bin/env bats
#
# Behavioural tests for scripts/bootstrap-machine.sh (Tier 1).
#
# Tier 1's contract is a DEPENDENCY, not a suggestion: it runs Tier 0 itself and
# refuses to continue if Tier 0 reports a missing prerequisite, then refuses to
# continue if any authenticated binary is absent. Both refusals used to be
# absent -- the old prereq block only PRINTED `MISSING:` and carried on, so
# every step below it ran against a machine known not to satisfy them.
#
# The real script is driven, never its source text. Tier 0 is replaced by a
# fixture in a COPY of the script's directory (bootstrap-machine.sh resolves its
# sibling via BASH_SOURCE), so these tests can force a Tier 0 failure without
# touching the real one -- and so a Tier 1 that stopped invoking Tier 0
# altogether would go red rather than quietly pass.
#
# NOTE: closing braces of @test blocks must stay at column 0 -- tests/bats-fallback.sh
# relies on that to run this file when the real bats binary is unavailable.

setup() {
  export REAL_T1="$BATS_TEST_DIRNAME/../scripts/bootstrap-machine.sh"
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"

  # A sandboxed copy of scripts/: the REAL Tier 1, a FAKE Tier 0 beside it.
  export SBOX="$BATS_TEST_TMPDIR/scripts"
  mkdir -p "$SBOX"
  cp "$REAL_T1" "$SBOX/bootstrap-machine.sh"
  export T1="$SBOX/bootstrap-machine.sh"
  export TIER0_LOG="$BATS_TEST_TMPDIR/tier0.log"

  # Keep Tier 1 away from the developer's real checkout and vault.
  export KHENRIX_REPO="$BATS_TEST_TMPDIR/repo"
  export OBSIDIAN_VAULT="$BATS_TEST_TMPDIR/vault"

  # Tier 1 runs with PATH=$STUB ONLY (see t1()), so binary presence is decided by
  # the fixture and not by the host: otherwise a missing-binary test would pass
  # vacuously on a developer machine that has all seven installed anyway.
  #
  # PATH is NOT exported here. setup() runs in the fallback harness's own shell
  # (it has to -- it exports what the bodies rely on), so clobbering PATH here
  # takes down the runner itself, mktemp/sed/rm and all.
  export STUB="$BATS_TEST_TMPDIR/stub"
  mkdir -p "$STUB"
  # The externals Tier 1 needs regardless of the fixture: `bash` because its
  # shebang is `#!/usr/bin/env bash` (env resolves it on the RESTRICTED path),
  # `dirname` to resolve its own directory, `grep` for the marketplace/plugin
  # presence checks. Deliberately absent: make, python3 -- under --dry-run they
  # are only ever echoed, so their absence proves nothing is executed for real.
  for b in bash dirname grep; do ln -sf "$(command -v "$b")" "$STUB/$b"; done
}

# Drive Tier 1 with ONLY the fixture PATH.
t1() {
  env PATH="$STUB" "$T1" "$@"
}

# A Tier 0 stand-in that records the arguments it was handed and exits $1.
plant_tier0() {
  cat > "$SBOX/bootstrap-tier0.sh" <<EOF
#!/usr/bin/env bash
printf 'TIER0-ARGS=[%s]\n' "\$*" >> "$TIER0_LOG"
echo "fake tier0 ran"
exit ${1:-0}
EOF
  chmod +x "$SBOX/bootstrap-tier0.sh"
}

# Put the seven authenticated binaries on PATH as no-op stubs.
plant_authenticated_bins() {
  for b in claude codex agy uv gh node git; do
    printf '#!/bin/sh\nexit 0\n' > "$STUB/$b"
    chmod +x "$STUB/$b"
  done
}

# ------------------------------------------------------- Tier 0 dependency ---

@test "Tier 1 ABORTS when Tier 0 reports a missing prerequisite" {
  plant_tier0 1
  plant_authenticated_bins
  run t1 --dry-run
  [ "$status" -eq 1 ]
  [[ "$output" == *"FATAL: Tier 0 reported missing prerequisites"* ]]
  # It must stop AT Tier 0 -- not merely mention it and press on.
  [[ "$output" != *"Tier 1 prereqs"* ]]
  [[ "$output" != *"marketplace"* ]]
}

@test "Tier 1 actually INVOKES Tier 0, it does not assume someone did" {
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dry-run
  [ "$status" -eq 0 ]
  [ -s "$TIER0_LOG" ]
  [[ "$output" == *"fake tier0 ran"* ]]
}

@test "--dry-run PROPAGATES into Tier 0 (a dry run must not provision)" {
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dry-run
  [ "$status" -eq 0 ]
  grep -qF 'TIER0-ARGS=[--dry-run]' "$TIER0_LOG"
}

@test "a REAL run hands Tier 0 no --dry-run" {
  # The mirror of the test above: if --dry-run were hardcoded into the Tier 0
  # call, the propagation test would still pass while Tier 0 never provisioned.
  #
  # CONTAINMENT: the authenticated binaries are deliberately NOT planted, so this
  # non-dry run reaches Tier 0 (which is the fixture) and is then killed by the
  # hard gate before it can execute a single mutating step. Nothing downstream
  # runs, and $HOME/$KHENRIX_REPO are temp dirs regardless.
  plant_tier0 0
  run t1
  [ "$status" -eq 1 ]
  grep -qF 'TIER0-ARGS=[]' "$TIER0_LOG"
}

@test "a missing Tier 0 script is FATAL, not a silently skipped step" {
  plant_authenticated_bins
  rm -f "$SBOX/bootstrap-tier0.sh"
  run t1 --dry-run
  [ "$status" -eq 1 ]
  [[ "$output" == *"Tier 0 missing or not executable"* ]]
}

# ------------------------------------------------- authenticated-tier gate ---

@test "a MISSING authenticated binary fails hard" {
  plant_tier0 0
  plant_authenticated_bins
  rm -f "$STUB/gh"
  run t1 --dry-run
  [ "$status" -eq 1 ]
  [[ "$output" == *"MISSING: gh"* ]]
  [[ "$output" == *"FATAL"* ]]
  # The old block printed MISSING: and kept going. Nothing after the gate may run.
  [[ "$output" != *"marketplace"* ]]
}

@test "EVERY authenticated binary is gated, not just the first" {
  plant_tier0 0
  for missing in claude codex agy uv gh node git; do
    plant_authenticated_bins
    rm -f "$STUB/$missing"
    run t1 --dry-run
    [ "$status" -eq 1 ]
    [[ "$output" == *"MISSING: $missing"* ]]
  done
}

@test "all seven present: the gate passes and the run proceeds" {
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"Tier 1 prereqs"* ]]
  for b in claude codex agy uv gh node git; do
    [[ "$output" == *"ok: $b"* ]]
  done
}

# --------------------------------------------------- wiring + dry-run purity --

@test "--dry-run wires reconcile --apply --all and doctor, mutating nothing" {
  # REGRESSION: both were commented out behind a 'confirm at T11' note, so a
  # bootstrap ended without applying config or proving anything about what it
  # built. Asserted as DRY: lines -- the run must PLAN them, not perform them.
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRY:"*"reconcile.py --apply --all"* ]]
  [[ "$output" == *"DRY:"*"doctor.py --profile full"* ]]
  [[ "$output" == *"Done (dry-run=1)"* ]]
  # Nothing may be executed for real.
  [[ "$output" != *"RUN:"* ]]
}

@test "doctor runs LAST, after reconcile -- it verifies the finished machine" {
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dry-run
  rec=$(printf '%s\n' "$output" | grep -n 'reconcile.py' | head -1 | cut -d: -f1)
  doc=$(printf '%s\n' "$output" | grep -n 'doctor.py'    | head -1 | cut -d: -f1)
  [ -n "$rec" ] && [ -n "$doc" ]
  [ "$doc" -gt "$rec" ]
}

@test "--dry-run creates no files at all" {
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dry-run
  [ "$status" -eq 0 ]
  [ ! -e "$KHENRIX_REPO" ]
  [ ! -e "$OBSIDIAN_VAULT" ]
  [ -z "$(find "$HOME" -mindepth 1 2>/dev/null)" ]
}

@test "an unknown argument is an ERROR, not a silent real run" {
  # `--dryrun` is a plausible typo for `--dry-run`, and Tier 1 mutates CLI
  # config -- falling through to a real run on a misspelt flag defeats shipping
  # a dry run at all. Tier 0 already refuses; Tier 1 has more to lose.
  plant_tier0 0
  plant_authenticated_bins
  run t1 --dryrun
  [ "$status" -eq 2 ]
  [[ "$output" == *"unknown argument"* ]]
  [ ! -s "$TIER0_LOG" ]
}
