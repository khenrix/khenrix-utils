#!/usr/bin/env bash
# repo-sweep.sh — classify repo-like directories with INDEPENDENT flags.
# Read-only. Never mutates anything. See spec 6d72793 §6.1.
#
# Detection rules that matter:
#   - repo-ness via `git rev-parse --git-dir`, NEVER `[ -d .git ]`
#     (a linked worktree's .git is a FILE)
#   - `ahead` is evaluated across ALL branches, not just HEAD
#   - flags are independent booleans; a dir can carry several
set -uo pipefail   # deliberately NOT -e: keep classifying past a failure

FORMAT=tsv
ROOTS=()
EXCLUDE_RE='/(node_modules|\.venv|\.cache|\.npm|\.tmp/plugins)(/|$)'

while [ $# -gt 0 ]; do
  case "$1" in
    --format) FORMAT="$2"; shift 2 ;;
    --roots)  shift; while [ $# -gt 0 ] && [[ "$1" != --* ]]; do ROOTS+=("$1"); shift; done ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ ${#ROOTS[@]} -eq 0 ] && ROOTS=("$HOME/git" "$HOME" "$HOME/.codex" "$HOME/.claude" "$HOME/.gemini")

classify() {
  local d="$1"; local -a f=()

  if ! git -C "$d" rev-parse --git-dir >/dev/null 2>&1; then
    printf 'not-a-repo'; return
  fi

  # worktree BEFORE anything that might suggest `git init`
  local gd cd_
  gd=$(git -C "$d" rev-parse --absolute-git-dir 2>/dev/null)
  cd_=$(git -C "$d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
  [ -f "$d/.git" ] || [ "$gd" != "$cd_" ] && f+=(worktree)

  local remotes; remotes=$(git -C "$d" remote 2>/dev/null)
  if [ -z "$remotes" ]; then
    f+=(no-remote)
  else
    # ahead: any commit on any local branch absent from every remote
    if [ -n "$(git -C "$d" log --oneline --branches --not --remotes 2>/dev/null | head -1)" ]; then
      f+=(ahead)
    fi
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

candidates() {
  local r
  for r in "${ROOTS[@]}"; do
    [ -d "$r" ] || continue
    find "$r" -maxdepth 2 -mindepth 1 -type d 2>/dev/null
    # linked worktrees can nest deeper (e.g. reponame/.claude/worktrees/name/.git
    # is 5 path components below the root -- maxdepth 4 is one short and
    # silently misses them; confirmed on this machine against a real
    # ~/git/hunter/.claude/worktrees/<name> worktree)
    find "$r" -maxdepth 5 -mindepth 1 -name .git -type f 2>/dev/null | xargs -r -n1 dirname
  done | grep -Ev "$EXCLUDE_RE" | sort -u
}

UNSAFE=0
declare -A SEEN_REPO
[ "$FORMAT" = tsv ] && printf 'path\tflags\tdetail\n'
while IFS= read -r d; do
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
  [[ "$flags" == *ahead* ]] && detail="$(git -C "$d" log --oneline --branches --not --remotes 2>/dev/null | wc -l) unpushed"
  if [ "$FORMAT" = human ]; then
    printf '%-52s %s %s\n' "$d" "$flags" "$detail"
  else
    printf '%s\t%s\t%s\n' "$d" "$flags" "$detail"
  fi
done < <(candidates)

exit "$UNSAFE"
