#!/usr/bin/env bash
# repo-sweep.sh — classify repo-like directories with INDEPENDENT flags.
# Read-only. Never mutates anything. See spec 6d72793 §6.1.
#
# This is the blocking safety gate before a WSL distro migration that destroys
# the old filesystem. A false "clean" loses work permanently, so every rule
# below errs toward reporting rather than staying quiet.
#
# Detection rules that matter:
#   - repo-ness via `git rev-parse --git-dir`, NEVER `[ -d .git ]`
#     (a linked worktree's .git is a FILE)
#   - `ahead` covers ALL local branches AND a detached HEAD. `git worktree add
#     <path> <commit>` leaves a DETACHED head, and commits made there are
#     reachable from no branch at all -- `--branches` alone cannot see them.
#   - flags are independent booleans; a dir can carry several
#
# Exit codes:
#   0  scanned, everything clean
#   1  scanned, at least one unsafe flag found
#   2  SCAN ERROR: bad arguments, a named root that does not exist or cannot be
#      read, or zero candidates produced.
#      (M5) Exit 2 is what lets a consumer distinguish "scanned and clean" from
#      "never scanned". Previously both were exit 0, so a typo'd root produced a
#      green safety gate. A green gate now REQUIRES that the scan ran and had
#      something to look at.
set -uo pipefail   # deliberately NOT -e: keep classifying past a failure

# ---------------------------------------------------------------------------
# WATCHLIST -- edit this to change where a missing repo is considered a problem.
#
# `not-a-repo` is only reported where a repo is genuinely EXPECTED. Flagging
# every ordinary folder produced 3256 of 3272 rows on a real run, which buried
# the ~15 actionable ones and made the plan's gate ("zero unsafe flags")
# unsatisfiable. Ordinary directories elsewhere are simply not reported.
#
# Every OTHER flag (ahead, no-upstream, dirty, has-stash, worktree, submodule,
# no-remote, broken-worktree) still applies to any repo found ANYWHERE.
# ---------------------------------------------------------------------------
# Every direct child of these directories is expected to be a repo:
EXPECT_PARENTS=("$HOME/git")
# These exact directories are expected to be repos:
EXPECT_DIRS=("$HOME/wedding" "$HOME/exercism" "$HOME/bin" "$HOME/docs")
# ---------------------------------------------------------------------------
# Test hooks: colon-separated overrides for the two lists above. Unset in
# normal use; the .bats suite sets them to point at its fixture tree.
if [ -n "${REPO_SWEEP_EXPECT_PARENTS+x}" ]; then
  IFS=: read -r -a EXPECT_PARENTS <<<"$REPO_SWEEP_EXPECT_PARENTS"
fi
if [ -n "${REPO_SWEEP_EXPECT_DIRS+x}" ]; then
  IFS=: read -r -a EXPECT_DIRS <<<"$REPO_SWEEP_EXPECT_DIRS"
fi

FORMAT=tsv
ROOTS=()
ROOTS_EXPLICIT=0
EXCLUDE_RE='/(node_modules|\.venv|\.cache|\.npm|\.tmp/plugins)(/|$)'
# Generous: ordinary clones nest arbitrarily (I1 -- a clone at depth 3 under a
# clean outer repo used to be invisible), and linked worktrees live at e.g.
# <repo>/.claude/worktrees/<name>/.git which is already 5 deep. Cost is low
# because the big offenders are pruned during the walk, not filtered after.
MAXDEPTH="${REPO_SWEEP_MAXDEPTH:-12}"

die() { echo "repo-sweep: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --format)
      [ $# -ge 2 ] || die "--format requires an argument (tsv|human)"
      FORMAT="$2"; shift 2 ;;
    --roots)
      shift
      ROOTS_EXPLICIT=1
      while [ $# -gt 0 ] && [[ "$1" != --* ]]; do ROOTS+=("$1"); shift; done
      # M4: --roots with nothing after it used to silently fall back to the
      # defaults, scanning the wrong tree under a green exit code.
      [ ${#ROOTS[@]} -gt 0 ] || die "--roots requires at least one directory" ;;
    *) die "unknown arg: $1" ;;
  esac
done

# M3: anything other than tsv|human used to fall through to the tsv branch
# without the header, producing output no consumer could parse.
case "$FORMAT" in
  tsv|human) ;;
  *) die "unknown --format: $FORMAT (expected tsv or human)" ;;
esac

[ ${#ROOTS[@]} -eq 0 ] && ROOTS=("$HOME/git" "$HOME" "$HOME/.codex" "$HOME/.claude" "$HOME/.gemini")

# Strip trailing slashes so path-prefix comparisons below are exact.
norm() { local p="$1"; while [ "$p" != "/" ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done; printf '%s' "$p"; }
for i in "${!ROOTS[@]}"; do ROOTS[$i]=$(norm "${ROOTS[$i]}"); done

# I2: a named root that does not exist, or that we cannot read, is a scan
# error -- not an empty-but-clean result. Default roots are allowed to be
# absent (not every machine has ~/.gemini); if they ALL are, the zero-candidate
# check below still catches it.
if [ "$ROOTS_EXPLICIT" -eq 1 ]; then
  for r in "${ROOTS[@]}"; do
    [ -d "$r" ] || die "root does not exist: $r"
    { [ -r "$r" ] && [ -x "$r" ]; } || die "root is not readable: $r"
  done
fi

is_under() {  # $1 under-or-equal-to $2
  [ "$1" = "$2" ] && return 0
  case "$1" in "$2"/*) return 0 ;; esac
  return 1
}

repo_expected() {  # is a repo expected to exist at $1?
  local d="$1" p
  for p in "${EXPECT_DIRS[@]}"; do [ "$d" = "$(norm "$p")" ] && return 0; done
  for p in "${EXPECT_PARENTS[@]}"; do [ "$(dirname "$d")" = "$(norm "$p")" ] && return 0; done
  # A directory holding a .git entry that git nonetheless rejects is corruption,
  # never an ordinary folder -- always worth a row wherever it lives.
  [ -e "$d/.git" ] && return 0
  return 1
}

classify() {
  local d="$1"; local -a f=()

  if ! git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
    # I3: a .git FILE that git rejects is a BROKEN LINKED WORKTREE (stale
    # gitdir pointer), not a plain directory. The remediation for `not-a-repo`
    # is `git init`, which would corrupt it -- so it gets its own label that no
    # consumer can mistake for an empty folder.
    if [ -f "$d/.git" ]; then printf 'broken-worktree'; else printf 'not-a-repo'; fi
    return
  fi

  # worktree/submodule BEFORE anything that might suggest `git init`.
  # Both have a .git FILE; only a linked worktree has its own gitdir distinct
  # from the common dir (M2 -- submodules used to be mislabelled `worktree`).
  local gd cd_
  gd=$(git -C "$d" rev-parse --absolute-git-dir 2>/dev/null)
  cd_=$(git -C "$d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
  if [ -n "$gd" ] && [ "$gd" != "$cd_" ]; then
    f+=(worktree)
  elif [ -f "$d/.git" ]; then
    f+=(submodule)
  fi

  local remotes; remotes=$(git -C "$d" remote 2>/dev/null)
  if [ -z "$remotes" ]; then
    f+=(no-remote)
  elif [ -n "$(unpushed "$d" | head -1)" ]; then
    f+=(ahead)
  fi

  # no-upstream: ANY local branch lacking an upstream
  if git -C "$d" for-each-ref --format='%(upstream)' refs/heads/ 2>/dev/null | grep -q '^$'; then
    f+=(no-upstream)
  fi

  [ -n "$(git -C "$d" status --porcelain 2>/dev/null | head -1)" ] && f+=(dirty)
  [ -n "$(git -C "$d" stash list 2>/dev/null | head -1)" ] && f+=(has-stash)

  [ ${#f[@]} -eq 0 ] && { printf 'clean'; return; }
  local IFS=,; printf '%s' "${f[*]}"
}

unpushed() {  # commits present locally but on no remote; one sha per line
  local d="$1"
  {
    # any commit on any local branch that no remote has
    git -C "$d" rev-list --branches --not --remotes 2>/dev/null
    # C1: ...plus commits reachable ONLY from HEAD. A detached HEAD (which is
    # what `git worktree add <path> <commit>` produces, and this machine has
    # three worktrees) is in refs/heads/ nowhere, so --branches walks straight
    # past it and the repo reports clean while holding unpushed work.
    git -C "$d" rev-list HEAD --not --branches --remotes 2>/dev/null
  } | sort -u
}

candidates() {
  local r p
  for r in "${ROOTS[@]}"; do
    [ -d "$r" ] || continue
    # Repo roots, at any depth: every .git entry (dir OR file) names one.
    # -printf '%h' instead of `xargs dirname` -- C3: xargs word-splits on
    # whitespace, so a worktree under ".../my repo/" was dropped entirely and a
    # truncated path got classified under the wrong label.
    find "$r" -mindepth 1 -maxdepth "$MAXDEPTH" \
      \( -type d \( -name node_modules -o -name .venv -o -name .cache -o -name .npm \) -prune \) -o \
      \( -name .git -prune -printf '%h\n' \) 2>/dev/null
    # ...plus the places where a repo is EXPECTED, so a missing one is visible.
    for p in "${EXPECT_PARENTS[@]}"; do
      p=$(norm "$p")
      is_under "$p" "$r" && find "$p" -mindepth 1 -maxdepth 1 -type d 2>/dev/null
    done
    for p in "${EXPECT_DIRS[@]}"; do
      p=$(norm "$p")
      is_under "$p" "$r" && [ -d "$p" ] && printf '%s\n' "$p"
    done
  done | grep -Ev "$EXCLUDE_RE" | sort -u
}

mapfile -t CANDS < <(candidates)

# I2: nothing scanned is a scan error, not "all clean".
[ ${#CANDS[@]} -eq 0 ] && die "no candidates found under: ${ROOTS[*]}"

UNSAFE=0
declare -A SEEN_REPO
[ "$FORMAT" = tsv ] && printf 'path\tflags\tdetail\n'
for d in "${CANDS[@]}"; do
  [ -d "$d" ] || continue

  # skip nested paths inside an already-reported repo, unless a worktree:
  # any subdirectory of a repo's working tree (its .git dir included)
  # independently satisfies `git rev-parse --git-dir` via upward discovery,
  # so without this a repo with N subdirectories produces N duplicate rows.
  # Key on --absolute-git-dir: it is the same path whether reached from the
  # worktree root, a subdirectory, or the .git dir itself, while still being
  # distinct per linked worktree (each has its own worktrees/<name> gitdir).
  if git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
    repo_key=$(git -C "$d" rev-parse --absolute-git-dir 2>/dev/null)
    if [ -n "$repo_key" ]; then
      [ -n "${SEEN_REPO[$repo_key]+x}" ] && continue
      SEEN_REPO["$repo_key"]=1
    fi
  fi

  flags=$(classify "$d")
  [ "$flags" = clean ] && continue          # only report actionable rows
  # I4: an ordinary non-repo directory outside the watchlist is not our problem.
  [ "$flags" = not-a-repo ] && ! repo_expected "$d" && continue
  UNSAFE=1
  detail=""
  [[ "$flags" == *ahead* ]] && detail="$(unpushed "$d" | wc -l) unpushed"
  if [ "$FORMAT" = human ]; then
    printf '%-52s %s %s\n' "$d" "$flags" "$detail"
  else
    printf '%s\t%s\t%s\n' "$d" "$flags" "$detail"
  fi
done

exit "$UNSAFE"
