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
  # The bare remote lives OUTSIDE $FIXT (the scanned root) on purpose. Bare
  # repos are candidates now (I5), and a bare repo with no remote of its own is
  # correctly unsafe -- so a remote parked inside the scan tree would add a row
  # to every fixture that calls this and make "all candidates clean" untestable.
  # A real clean clone's remote is on a server, not in the tree being migrated.
  local bare="$BATS_TEST_TMPDIR/remotes/$2"; mkdir -p "$(dirname "$bare")"
  git init -q --bare "$bare"
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
  bare="$BATS_TEST_TMPDIR/remotes/inner-bare.git"   # outside $FIXT -- see mkremote
  mkdir -p "$(dirname "$bare")"; git init -q --bare "$bare"
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

@test "not-a-repo is reported inside the watchlist and absent outside it" {
  # I4: flagging every ordinary folder buried the ~15 actionable rows under 3256.
  #
  # This test previously asserted only the ABSENT half, with setup() having
  # unset the watchlist hooks -- so its fixture was never a candidate and the
  # assertion held for the wrong reason. It passed unchanged when the filter it
  # claimed to cover was deleted. Both halves are now exercised in one scan with
  # the watchlist actually pointed at the fixture, so the reported half fails if
  # scoping is over-tightened and the absent half fails if it is loosened.
  mkdir -p "$FIXT/watched/missing" "$FIXT/elsewhere/ordinary"
  export REPO_SWEEP_EXPECT_PARENTS="$FIXT/watched"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ "$(flags_for "$FIXT/watched/missing")" = "not-a-repo" ]
  [ -z "$(flags_for "$FIXT/elsewhere/ordinary")" ]
}

@test "bare repo nested below the scan root is found, never invisible" {
  # REGRESSION: candidates() emitted only the dirname of each .git entry, and a
  # bare repo HAS no .git entry -- so a bare repo holding the only copy of a
  # commit produced zero rows and exit 0. Reviewer's fixture shape: a clean
  # pushed repo beside a bare repo one level deeper than any watchlist entry,
  # which is the arrangement no other rule catches.
  d=$(mkrepo clean)
  mkremote "$d" clean-bare.git
  mkdir -p "$FIXT/archive"
  bare="$FIXT/archive/only-copy.git"
  git init -q --bare "$bare"
  wc="$BATS_TEST_TMPDIR/wc"; git clone -q "$bare" "$wc" 2>/dev/null
  gitq "$wc" commit -q --allow-empty -m "UNIQUE WORK"
  git -C "$wc" push -q origin HEAD:refs/heads/main
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$(flags_for "$bare")" == *bare* ]]
  [[ "$(flags_for "$bare")" == *no-remote* ]]
  [ "$status" -eq 1 ]
  # The bare repo itself, not its internals: its own logs/HEAD must not nominate
  # logs/ as a second, bogus candidate.
  [ -z "$(flags_for "$bare/logs")" ]
}

@test "a stray file named HEAD does not nominate its directory as a repo" {
  # Bare-repo discovery keys off HEAD files, so it must CONFIRM with git rather
  # than trust the filename. Without that check any directory holding an
  # unrelated file called HEAD becomes a candidate and, having no .git, reports
  # not-a-repo -- reintroducing exactly the noise the I4 scoping removed.
  d=$(mkrepo anchor)
  mkremote "$d" anchor-bare.git
  mkdir -p "$FIXT/notes"
  echo "release notes" > "$FIXT/notes/HEAD"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ -z "$(flags_for "$FIXT/notes")" ]
  [ "$status" -eq 0 ]
}

@test "unreadable subtree inside a readable root is a scan error, not all-clean" {
  # A directory we cannot enter may hold anything, including the only copy of
  # some work -- here a repo with an unpushed commit. `find` reports it on
  # stderr; discarding that turned a blind spot into a green gate.
  d=$(mkrepo anchor)                # readable, clean: the scan is otherwise fine
  mkremote "$d" anchor-bare.git
  mkdir -p "$FIXT/locked/repo"
  git -C "$FIXT/locked/repo" init -q
  gitq "$FIXT/locked/repo" commit -q --allow-empty -m "UNPUSHED WORK"
  chmod 000 "$FIXT/locked"
  run "$SWEEP" --format tsv --roots "$FIXT"
  chmod 755 "$FIXT/locked"          # before asserting, so a failure still cleans up
  [ "$status" -eq 2 ]
}

@test "unreadable DEFAULT root is a scan error, not all-clean" {
  # I2 validated named roots only, and only when --roots was passed explicitly,
  # so the production default-roots path -- the one the migration gate actually
  # runs -- validated nothing at all.
  mkdir -p "$FIXT/home/git"
  chmod 000 "$FIXT/home/git"
  run env HOME="$FIXT/home" "$SWEEP" --format tsv
  chmod 755 "$FIXT/home/git"
  [ "$status" -eq 2 ]
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
