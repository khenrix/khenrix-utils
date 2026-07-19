#!/usr/bin/env bats

setup() {
  export FIXT="$BATS_TEST_TMPDIR/fixt"
  mkdir -p "$FIXT"
  export SWEEP="$BATS_TEST_DIRNAME/../scripts/repo-sweep.sh"
  git config --global init.defaultBranch main 2>/dev/null || true
}

mkrepo() {  # $1 = name
  local d="$FIXT/$1"; mkdir -p "$d"; git -C "$d" init -q
  git -C "$d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  echo "$d"
}

@test "plain directory is flagged not-a-repo" {
  mkdir -p "$FIXT/plain"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$output" == *"plain"*"not-a-repo"* ]]
}

@test "repo with no remote is flagged no-remote, not not-a-repo" {
  mkrepo solo >/dev/null
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$output" == *"solo"*"no-remote"* ]]
  [[ "$output" != *"solo"*"not-a-repo"* ]]
}

@test "linked worktree is flagged worktree and NOT not-a-repo" {
  d=$(mkrepo wtmain)
  git -C "$d" branch -q side
  git -C "$d" worktree add -q "$FIXT/wtlinked" side
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$output" == *"wtlinked"*"worktree"* ]]
  [[ "$output" != *"wtlinked"*"not-a-repo"* ]]
}

@test "branch without upstream is flagged no-upstream even when HEAD has one" {
  d=$(mkrepo up)
  bare="$FIXT/up-bare.git"; git init -q --bare "$bare"
  git -C "$d" remote add origin "$bare"
  git -C "$d" push -q -u origin main
  git -C "$d" branch -q orphan-work
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$output" == *"up"*"no-upstream"* ]]
}

@test "unpushed commit on a NON-checked-out branch is flagged ahead" {
  d=$(mkrepo ahead)
  bare="$FIXT/ahead-bare.git"; git init -q --bare "$bare"
  git -C "$d" remote add origin "$bare"
  git -C "$d" push -q -u origin main
  git -C "$d" checkout -q -b other
  git -C "$d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m unpushed
  git -C "$d" checkout -q main
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$output" == *"ahead"*"ahead"* ]]
}

@test "stash is flagged has-stash" {
  d=$(mkrepo st)
  echo change > "$d/f"; git -C "$d" add f
  git -C "$d" -c user.email=t@t -c user.name=t stash -q
  run "$SWEEP" --format tsv --roots "$FIXT"
  [[ "$output" == *"st"*"has-stash"* ]]
}

@test "exit code is 1 when any unsafe flag present" {
  mkdir -p "$FIXT/plain"
  run "$SWEEP" --format tsv --roots "$FIXT"
  [ "$status" -eq 1 ]
}
