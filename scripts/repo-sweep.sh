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
#   - `ahead` covers EVERY local ref (not just branches) AND a detached HEAD.
#     `git worktree add <path> <commit>` leaves a DETACHED head, and a commit can
#     also survive on a tag or an arbitrary `refs/archive/...` ref after its
#     branch is deleted -- `--branches` alone sees none of those. See unpushed().
#   - a repo-like thing git REFUSES to open is labelled `broken-*`, never dropped
#     and never silently skipped. Dropping it is a false clean; that is the whole
#     failure mode this gate exists to prevent.
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
# Antigravity CLI keeps one throwaway git repo per session under
# .gemini/antigravity-cli/brain/<uuid> (441 of them / 227MB here). They are
# machine-generated state -- every .user_uploaded/ is empty, contents are
# system logs -- so they are noise in a sweep for at-risk USER work. They are
# still captured by the home archive via ~/.gemini; excluding them here only
# affects reporting.
EXCLUDE_RE='/(node_modules|\.venv|\.cache|\.npm|\.tmp/plugins|\.gemini/antigravity-cli/brain)(/|$)'
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

# I2: a root that does not exist, or that we cannot read, is a scan error --
# not an empty-but-clean result.
#
# (I6) This used to run only when --roots was passed, so the PRODUCTION
# default-roots path validated nothing at all: an unreadable ~/git produced a
# green gate. Existence stays conditional -- a NAMED root that is absent is a
# typo, while a default root may legitimately be absent (not every machine has
# ~/.gemini) -- but READABILITY is now enforced on both paths. An existing root
# we cannot open is never safe to treat as empty.
for r in "${ROOTS[@]}"; do
  if [ ! -d "$r" ]; then
    [ "$ROOTS_EXPLICIT" -eq 1 ] && die "root does not exist: $r"
    continue
  fi
  { [ -r "$r" ] && [ -x "$r" ]; } || die "root is not readable: $r"
done

# (I6) Unreadable subtrees found mid-walk are a scan error too. `find` reports
# them on stderr; the old `2>/dev/null` discarded that, so a mode-000 directory
# holding unpushed work scanned as clean. We test only whether stderr is
# NON-EMPTY, never its wording: /usr/bin/find here is bfs, whose message format
# differs from GNU find's, and neither is a stable interface.
SCAN_ERR=$(mktemp)
trap 'rm -f "$SCAN_ERR"' EXIT

is_under() {  # $1 under-or-equal-to $2
  [ "$1" = "$2" ] && return 0
  case "$1" in "$2"/*) return 0 ;; esac
  return 1
}

# NOTE (I4): there is deliberately no `repo_expected()` filter here any more.
# `not-a-repo` scoping is enforced UPSTREAM, by candidates() simply never
# nominating ordinary directories -- a candidate is a dir holding a .git entry,
# a confirmed bare repo, or a watchlist entry, and nothing else. A post-hoc
# filter re-testing the watchlist was therefore vacuous by construction: every
# candidate that can classify `not-a-repo` satisfied it. Instrumenting the
# branch confirmed it suppressed 0 rows across the whole suite and 0 on the
# real machine, and deleting it left all tests green -- dead code behind a
# passing test. Test "not-a-repo is reported inside the watchlist..." now
# exercises the real mechanism instead.

classify() {
  local d="$1"; local -a f=()

  if ! git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
    # I3: a .git FILE that git rejects is a BROKEN LINKED WORKTREE (stale
    # gitdir pointer), not a plain directory. The remediation for `not-a-repo`
    # is `git init`, which would corrupt it -- so it gets its own label that no
    # consumer can mistake for an empty folder.
    if [ -f "$d/.git" ]; then printf 'broken-worktree'; return; fi
    # I7: likewise a BARE repo git refuses to open. It has no `.git` entry at
    # all, so without this it fell through to `not-a-repo` -- or, before the
    # `--is-bare-repository` guard was dropped from candidates(), was dropped
    # from the scan entirely and produced exit 0 while its objects sat on disk.
    # A uid/`safe.directory` mismatch is the realistic cause AND is exactly what
    # a WSL distro migration produces, so this is the likeliest corruption here,
    # not a contrived one. Structural signature only -- git is by definition
    # unavailable as the authority once it has refused to open the thing.
    if [ -d "$d/objects" ] && [ -d "$d/refs" ] && [ -f "$d/HEAD" ]; then
      printf 'broken-bare-repo'; return
    fi
    printf 'not-a-repo'
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

  # I5: a BARE repo has no working tree, so `dirty`/`has-stash` can never fire
  # for it and its remediation differs (you push FROM it; `git init` is wrong).
  # Label it so a row like `archive/only-copy.git  no-remote` is not misread as
  # an ordinary clone whose checkout lives somewhere safe.
  [ "$(git -C "$d" rev-parse --is-bare-repository 2>/dev/null)" = true ] && f+=(bare)

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
  local -a lrefs=()
  # C4: every LOCAL ref, not just refs/heads/. Three distinct kinds of local-only
  # work live outside refs/heads/, and `--branches` sees none of them:
  #   * a commit reachable only from a TAG -- commit on a topic branch, `git tag
  #     keepme`, checkout main, delete the branch. On no branch, not HEAD, on no
  #     remote: previously zero rows and exit 0.
  #   * a commit under an arbitrary ref such as refs/archive/... written by
  #     `git update-ref` -- same invisibility.
  #   * a DETACHED HEAD (C1), handled by the second walk below.
  # Enumerating refs covers the first two; `--branches --tags` would have covered
  # only the tag case and left refs/archive/... silently uncovered.
  #
  # refs/stash is the ONE deliberate exclusion, and the reason `--all` is wrong
  # here: a stash commit drags in its index/untracked parent commits, which on
  # this machine gave ~/git/fairshare a spurious `ahead 2 unpushed` that was
  # nothing but its own stash. A false alarm erodes trust in a blocking gate.
  # Nothing is lost: a repo holding a stash already reports the INDEPENDENT
  # `has-stash` flag, so it is still unsafe and still reported.
  mapfile -t lrefs < <(git -C "$d" for-each-ref --format='%(refname)' 2>/dev/null \
    | grep -Ev '^refs/(remotes|stash)($|/)')
  {
    [ ${#lrefs[@]} -gt 0 ] && git -C "$d" rev-list "${lrefs[@]}" --not --remotes 2>/dev/null
    # C1: ...plus commits reachable ONLY from HEAD. A detached HEAD (which is
    # what `git worktree add <path> <commit>` produces, and this machine has
    # three worktrees) is in refs/heads/ nowhere, so the ref walk above misses
    # it. Kept as a SEPARATE rev-list rather than appended to "${lrefs[@]}": in a
    # repo whose HEAD is unborn, naming HEAD makes rev-list fail outright and
    # discard the ref walk's output with it -- failing OPEN, the one direction
    # this gate must never fail.
    git -C "$d" rev-list HEAD --not --remotes 2>/dev/null
  } | sort -u
}

candidates() {
  local r p line d
  for r in "${ROOTS[@]}"; do
    [ -d "$r" ] || continue
    # ONE walk per root, emitting two kinds of hit:
    #   g <dir>  dir holds a .git entry (dir OR file) -- an ordinary repo root
    #   h <dir>  dir holds a file named HEAD -- a POSSIBLE bare repo top level
    #
    # I5 (REGRESSION): keying discovery off `.git` alone made every bare repo
    # invisible. A bare repo has no `.git` entry at all, so a bare repo holding
    # the only copy of a commit produced zero rows and exit 0 -- precisely the
    # false clean this gate exists to prevent. Its top level instead holds HEAD,
    # objects/ and refs/. Matching the HEAD *file* is name-independent, so it
    # also catches bare repos NOT named *.git, which `-name '*.git'` would miss.
    #
    # -printf '%h' instead of `xargs dirname` -- C3: xargs word-splits on
    # whitespace, so a worktree under ".../my repo/" was dropped entirely and a
    # truncated path got classified under the wrong label.
    while IFS= read -r line; do
      d="${line:2}"
      case "$line" in
        'g '*) printf '%s\n' "$d" ;;
        'h '*)
          # Confirm rather than trust the filename: a stray file called HEAD is
          # not a repo, and a bare repo's own logs/HEAD would otherwise nominate
          # its logs/ directory. The STRUCTURAL check is the whole confirmation.
          #
          # I7: there used to be a second `--is-bare-repository || continue`
          # guard here, and it made this path fail OPEN while every other path
          # fails closed. Anything git refuses to open -- a uid/`safe.directory`
          # mismatch, which is precisely what a WSL migration produces -- was
          # silently dropped, so a bare repo holding the only copy of a commit
          # yielded zero rows and exit 0. The same corruption on a NON-bare repo
          # correctly reported `not-a-repo` and exit 1; this path was the
          # outlier. Nominating it unconditionally lets classify() label it
          # `broken-bare-repo` instead. The structural check alone already
          # rejects every noise case (logs/, refs/, objects/, a stray HEAD file),
          # and adds zero rows on the real machine.
          [ -d "$d/objects" ] && [ -d "$d/refs" ] || continue
          printf '%s\n' "$d" ;;
      esac
    done < <(find "$r" -mindepth 1 -maxdepth "$MAXDEPTH" \
      \( -type d \( -name node_modules -o -name .venv -o -name .cache -o -name .npm \) -prune \) -o \
      \( -name .git -prune -printf 'g %h\n' \) -o \
      \( -type f -name HEAD -printf 'h %h\n' \) 2>>"$SCAN_ERR")
    # ...plus the places where a repo is EXPECTED, so a missing one is visible.
    for p in "${EXPECT_PARENTS[@]}"; do
      p=$(norm "$p")
      is_under "$p" "$r" && find "$p" -mindepth 1 -maxdepth 1 -type d 2>>"$SCAN_ERR"
    done
    for p in "${EXPECT_DIRS[@]}"; do
      p=$(norm "$p")
      is_under "$p" "$r" && [ -d "$p" ] && printf '%s\n' "$p"
    done
  done | grep -Ev "$EXCLUDE_RE" | sort -u
}

mapfile -t CANDS < <(candidates)

# (I6) A subtree we could not enter may hold anything, including the only copy
# of some work. An incomplete scan is a scan error -- reporting it as clean is
# the failure mode this gate exists to prevent, so this fails CLOSED.
if [ -s "$SCAN_ERR" ]; then
  die "scan incomplete, some paths could not be read:
$(sed 's/^/  /' "$SCAN_ERR")"
fi

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
