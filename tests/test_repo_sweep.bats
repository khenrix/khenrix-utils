#!/usr/bin/env bats
#
# Assertions here are FIELD-TARGETED on purpose (finding M1/C2): every check
# extracts the flags column of one exact row with awk instead of substring-
# matching the whole output. The old `[[ "$output" == *"ahead"*"ahead"* ]]`
# style passed even when the feature under test was completely broken, because
# unrelated rows (and the header) supplied the substrings.
#
# NOTE: closing braces of @test blocks must stay at column 0 -- tests/bats-fallback.sh
# relies on that to run this file when the real bats binary is unavailable.

setup() {
  export FIXT="$BATS_TEST_TMPDIR/fixt"
  mkdir -p "$FIXT"
  export SWEEP="$BATS_TEST_DIRNAME/../scripts/repo-sweep.sh"
  git config --global init.defaultBranch main 2>/dev/null || true
  # The watchlist is off by default so `not-a-repo` scoping (I4) is testable;
  # tests that need it point it at the fixture tree themselves.
  unset REPO_SWEEP_EXPECT_PARENTS REPO_SWEEP_EXPECT_DIRS
}

gitq() { git -C "$1" -c user.email=t@t -c user.name=t "${@:2}"; }

mkrepo() {  # $1 = name
  local d="$FIXT/$1"; mkdir -p "$d"; git -C "$d" init -q
  gitq "$d" commit -q --allow-empty -m init
  echo "$d"
}

mkremote() {  # $1 = repo dir, $2 = bare name -- pushes main with upstream set
  local bare="$FIXT/$2"; git init -q --bare "$bare"
  git -C "$1" remote add origin "$bare"
  git -C "$1" push -q -u origin main
}

flags_for() {  # $1 = exact path -> that row's flags field ("" if no such row)
  awk -F'\t' -v d="$1" '$1==d {print $2}' <<<"$output"
}

detail_for() {  # $1 = exact path -> that row's detail field
  awk -F'\t' -v d="$1" '$1==d {print $3}' <<<"$output"
}

@test "plain directory is flagged not-a-repo" {
  mkdir -p "$FIXT/plain"
  export REPO_SWEEP_EXPECT_PARENTS="$FIXT"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ "$(flags_for "$FIXT/plain")" = "not-a-repo" ]
}

@test "repo with no remote is flagged no-remote, not not-a-repo" {
  d=$(mkrepo solo)
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$d")" == *no-remote* ]]
  [[ "$(flags_for "$d")" != *not-a-repo* ]]
}

@test "linked worktree is flagged worktree and NOT not-a-repo" {
  d=$(mkrepo wtmain)
  git -C "$d" branch -q side
  git -C "$d" worktree add -q "$FIXT/wtlinked" side
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$FIXT/wtlinked")" == *worktree* ]]
  [[ "$(flags_for "$FIXT/wtlinked")" != *not-a-repo* ]]
}

@test "branch without upstream is flagged no-upstream even when HEAD has one" {
  d=$(mkrepo up)
  mkremote "$d" up-bare.git
  git -C "$d" branch -q orphan-work
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$d")" == *no-upstream* ]]
}

@test "unpushed commit on a NON-checked-out branch is flagged ahead" {
  d=$(mkrepo ahead)
  mkremote "$d" ahead-bare.git
  git -C "$d" checkout -q -b other
  gitq "$d" commit -q --allow-empty -m unpushed
  git -C "$d" checkout -q main
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$d")" == *ahead* ]]
  [ "$(detail_for "$d")" = "1 unpushed" ]
}

@test "unpushed commit on a DETACHED HEAD is flagged ahead" {
  # C1: `git worktree add <path> <commit>` leaves a detached HEAD, and
  # `--branches` walks refs/heads/ only -- so this used to report clean.
  d=$(mkrepo det)
  mkremote "$d" det-bare.git
  git -C "$d" checkout -q --detach
  gitq "$d" commit -q --allow-empty -m "detached work"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$d")" == *ahead* ]]
  [ "$(detail_for "$d")" = "1 unpushed" ]
}

@test "uncommitted change is flagged dirty" {
  d=$(mkrepo dty)
  mkremote "$d" dty-bare.git
  echo change > "$d/f"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$d")" == *dirty* ]]
}

@test "stash is flagged has-stash" {
  d=$(mkrepo st)
  echo change > "$d/f"; git -C "$d" add f
  gitq "$d" stash -q
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$d")" == *has-stash* ]]
}

@test "worktree under a path containing a space is still found" {
  # C3: `find ... | xargs -n1 dirname` word-splits, silently dropping this
  # worktree and inventing a truncated path in its place.
  mkdir -p "$FIXT/my repo"
  d=$(mkrepo "my repo/main")
  git -C "$d" branch -q side
  git -C "$d" worktree add -q "$FIXT/my repo/linked" side
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$FIXT/my repo/linked")" == *worktree* ]]
  [[ "$output" != *"$FIXT/my"$'\t'* ]]
}

@test "ordinary clone nested 3+ levels deep is classified" {
  # I1: -maxdepth 2 for dirs plus a second pass restricted to .git FILES meant
  # deep ordinary clones were never candidates at all.
  outer=$(mkrepo outer)
  mkremote "$outer" outer-bare.git
  inner="$outer/a/b/inner"; mkdir -p "$inner"
  git -C "$inner" init -q
  gitq "$inner" commit -q --allow-empty -m init
  bare="$FIXT/inner-bare.git"; git init -q --bare "$bare"
  git -C "$inner" remote add origin "$bare"
  git -C "$inner" push -q -u origin main
  gitq "$inner" commit -q --allow-empty -m unpushed
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$inner")" == *ahead* ]]
}

@test "broken linked worktree is broken-worktree, never not-a-repo" {
  # I3: the remediation for not-a-repo is `git init`, which would corrupt this.
  d=$(mkrepo bw)
  git -C "$d" branch -q side
  git -C "$d" worktree add -q "$FIXT/bwlinked" side
  printf 'gitdir: %s\n' "$FIXT/gone/worktrees/bwlinked" > "$FIXT/bwlinked/.git"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ "$(flags_for "$FIXT/bwlinked")" = "broken-worktree" ]
}

@test "submodule is flagged submodule, not worktree" {
  # M2: a submodule's .git is a FILE too, but its gitdir == its common dir.
  sub=$(mkrepo subsrc)
  par=$(mkrepo parent)
  gitq "$par" -c protocol.file.allow=always submodule add -q "$sub" vendor
  gitq "$par" commit -q -m addsub
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$par/vendor")" == *submodule* ]]
  [[ "$(flags_for "$par/vendor")" != *worktree* ]]
}

@test "non-repo directory outside the watchlist produces no row" {
  # I4: flagging every ordinary folder buried the ~15 actionable rows under 3256.
  mkrepo anchor >/dev/null          # keeps the scan non-empty (see exit-2 tests)
  mkdir -p "$FIXT/ordinary"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ -z "$(flags_for "$FIXT/ordinary")" ]
}

@test "exit code is 1 when any unsafe flag present" {
  mkdir -p "$FIXT/plain"
  export REPO_SWEEP_EXPECT_PARENTS="$FIXT"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ "$status" -eq 1 ]
}

@test "exit code is 0 when every candidate is clean" {
  d=$(mkrepo cln)
  mkremote "$d" cln-bare.git
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ "$status" -eq 0 ]
}

@test "nonexistent root is a scan error, not all-clean" {
  run "$SWEEP" --format tsv --roots "$FIXT/does-not-exist"
  [ "$status" -eq 2 ]
}

@test "empty root is a scan error, not all-clean" {
  mkdir -p "$FIXT/empty"
  run "$SWEEP" --format tsv --roots "$FIXT/empty"
  [ "$status" -eq 2 ]
}

@test "unreadable root is a scan error, not all-clean" {
  mkdir -p "$FIXT/locked"; chmod 000 "$FIXT/locked"
  run "$SWEEP" --format tsv --roots "$FIXT/locked"
  chmod 755 "$FIXT/locked"
  [ "$status" -eq 2 ]
}

@test "invalid --format is rejected" {
  mkrepo fmt >/dev/null
  run "$SWEEP" --format json --roots "$FIXT"
  [ "$status" -eq 2 ]
}

@test "--roots with no directories is rejected" {
  run "$SWEEP" --format tsv --roots
  [ "$status" -eq 2 ]
}
