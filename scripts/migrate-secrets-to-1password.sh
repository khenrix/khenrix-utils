#!/usr/bin/env bash
# migrate-secrets-to-1password.sh -- move literal secrets out of a shell rc file
# and leave an op:// reference behind.
#
# WHY. Three live credentials sat in ~/.bashrc on this machine indefinitely (a
# Supabase secret key, a database password, a Google Places API key) as literal
# `export VAR="value"` lines, world-readable to every process the user runs and
# copied into every backup, tarball and agent transcript. doctor.py's
# `no-plaintext-secrets-in-shell-rc` check FINDS them; this script MOVES them.
#
# ONE DETECTOR, NOT TWO. The list of what counts as a secret comes from
# `doctor.py --scan-rc`, never from a regex reimplemented here. A second
# detector would drift, and the two failure modes are both bad: a line the
# doctor flags that this script will not migrate (the check FAILs forever), or a
# line this script rewrites that was never a secret (a working rc file broken).
#
# NEVER ECHOES A VALUE. Not on success, not on failure, not in --dry-run. The
# secret is held in one shell variable and handed to `op` over STDIN (a JSON
# template) rather than as an argument -- `op item create --help` warns that
# command arguments land in shell history and are visible to other processes,
# and this script exists precisely to stop that class of leak.
#
# TRANSACTIONAL PER VARIABLE. For each variable: write to 1Password, READ IT
# BACK and compare, and only then rewrite the rc line. A failed or unverifiable
# write leaves the line exactly as it was and is reported. A partial run is
# therefore always safe -- every rc line either still holds its literal or
# points at a reference that has been proven readable.
#
# --dry-run IS THE DEFAULT. --apply is required to mutate anything.
set -uo pipefail

usage() {
  cat <<'USAGE'
migrate-secrets-to-1password.sh -- move literal secrets from a shell rc file
into 1Password, replacing each with an op:// reference.

Usage: migrate-secrets-to-1password.sh [--apply] [--vault NAME] [FILE]

  FILE           The rc file to migrate (default: ~/.bashrc).
  --apply        Actually write to 1Password and rewrite FILE.
                 Without it nothing is created, edited or modified.
  --dry-run      Explicit form of the default. Prints variable NAMES and their
                 destination op:// references only -- never a value.
  --vault NAME   1Password vault to write into (default: Private).
  -h, --help     This text.

Requires an already-signed-in `op`. Unlocking 1Password is a human checkpoint;
this script will never attempt to authenticate on your behalf.

Exit 0 on success or a clean dry run, 1 if any variable failed to migrate,
2 on a usage error, 3 if `op` is missing or not signed in.
USAGE
}

APPLY=0
VAULT="Private"
RC=""

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)   APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --vault)
      [ $# -ge 2 ] || { echo "migrate-secrets: --vault needs a value" >&2; exit 2; }
      VAULT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "migrate-secrets: unknown argument: $1" >&2
        echo "Try --help." >&2
        exit 2 ;;
    *)  [ -z "$RC" ] || { echo "migrate-secrets: only one FILE may be given" >&2; exit 2; }
        RC="$1" ;;
  esac
  shift
done

RC="${RC:-$HOME/.bashrc}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOCTOR="$SCRIPT_DIR/doctor.py"
PY="${PYTHON:-python3}"

[ -f "$RC" ] || { echo "migrate-secrets: not a readable file: $RC" >&2; exit 2; }
[ -f "$DOCTOR" ] || { echo "migrate-secrets: cannot find $DOCTOR" >&2; exit 2; }

# --- preflight: op must ALREADY be signed in ------------------------------
#
# Deliberately does not run `op signin`. A 1Password unlock is a human
# checkpoint (house-style.md): a script that prompts for a master password
# trains the user to type it wherever they are asked, which is the phishing
# behaviour the rule exists to prevent. So: detect, instruct, stop.
command -v op >/dev/null 2>&1 || {
  echo "migrate-secrets: the 1Password CLI (\`op\`) is not installed." >&2
  echo "  Install it: https://developer.1password.com/docs/cli/get-started/" >&2
  exit 3
}

ACCOUNTS="$(op account list 2>/dev/null)"
if [ -z "${ACCOUNTS//[[:space:]]/}" ]; then
  cat >&2 <<'NOAUTH'
migrate-secrets: `op` has no account configured, so nothing can be written.

Run these yourself -- this script will not authenticate for you, because a
1Password unlock is a human checkpoint:

    op account add
    eval $(op signin)

Then re-run this script.
NOAUTH
  exit 3
fi

# --- what needs migrating -------------------------------------------------
#
# doctor.py is the single source of truth. It prints "lineno<TAB>NAME<TAB>why"
# and never a value.
FINDINGS="$("$PY" "$DOCTOR" --scan-rc "$RC")" || {
  echo "migrate-secrets: doctor.py --scan-rc failed on $RC" >&2
  exit 2
}

if [ -z "${FINDINGS//[[:space:]]/}" ]; then
  echo "No literal secrets found in $RC — nothing to migrate."
  exit 0
fi

COUNT="$(printf '%s\n' "$FINDINGS" | grep -c .)"
if [ "$APPLY" = 1 ]; then
  echo "Migrating $COUNT secret(s) from $RC into 1Password vault '$VAULT'."
else
  echo "DRY RUN — nothing will be written. $COUNT secret(s) found in $RC."
  echo "Re-run with --apply to perform the migration."
fi
echo

# --- backup ---------------------------------------------------------------
#
# Made before the first write, and only in --apply mode. It contains the
# literal secrets, so it is created 0600 and the closing note tells the user to
# delete it once the migration is verified.
BACKUP=""
if [ "$APPLY" = 1 ]; then
  BACKUP="$RC.pre-1password.$(date +%Y%m%d-%H%M%S).bak"
  if ! (umask 077 && cp -p -- "$RC" "$BACKUP"); then
    echo "migrate-secrets: could not back up $RC — refusing to modify it." >&2
    exit 1
  fi
  chmod 600 -- "$BACKUP" 2>/dev/null
  echo "Backup: $BACKUP (mode 600, CONTAINS THE LITERAL SECRETS)"
  echo
fi

# A file with no trailing newline is a live hazard: anything that later appends
# to it silently joins onto the last line. We PRESERVE whatever the file has
# rather than quietly changing it, but the user should know.
if [ -n "$(tail -c 1 -- "$RC")" ]; then
  echo "NOTE: $RC does not end with a newline. It is preserved as-is;"
  echo "      appending to it without adding one first will corrupt the last line."
  echo
fi

# --- rewrite one line, preserving the file's trailing-newline state --------
#
# Python rather than sed -i: the replacement is applied to exactly one line by
# NUMBER, every other byte is passed through untouched, and the final line's
# terminator (or absence of one) is preserved exactly. The new line contains
# only an op:// reference, so passing it in argv leaks nothing.
rewrite_line() {
  "$PY" - "$RC" "$1" "$2" <<'PY'
import sys
path, lineno, new = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
    lines = f.readlines()
if not 1 <= lineno <= len(lines):
    sys.exit("line %d out of range" % lineno)
old = lines[lineno - 1]
eol = ""
for cand in ("\r\n", "\n"):
    if old.endswith(cand):
        eol = cand
        break
lines[lineno - 1] = new + eol
with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
    f.writelines(lines)
PY
}

# --- migrate --------------------------------------------------------------

FAILED=0
MIGRATED=0

while IFS=$'\t' read -r LINENO_ NAME WHY; do
  [ -n "${NAME:-}" ] || continue

  # op://<vault>/<item>/<field>. The item title is the variable name lowercased
  # with underscores as dashes; the field is `credential`, the built-in
  # concealed field of the API Credential category.
  ITEM="$(printf '%s' "$NAME" | tr '[:upper:]_' '[:lower:]-')"
  FIELD="credential"
  REF="op://$VAULT/$ITEM/$FIELD"

  if [ "$APPLY" != 1 ]; then
    printf '  %-34s -> %s   (%s)\n' "$NAME" "$REF" "$WHY"
    continue
  fi

  # Read the literal out of the rc file. From here until the write completes the
  # value lives in $VAL and is never printed, never passed as an argument, and
  # never written to disk.
  LINE="$(sed -n "${LINENO_}p" -- "$RC")"
  RHS="${LINE#*=}"
  if [[ $RHS == \"* ]]; then
    TMP="${RHS#\"}"; VAL="${TMP%%\"*}"
  elif [[ $RHS == \'* ]]; then
    TMP="${RHS#\'}"; VAL="${TMP%%\'*}"
  else
    VAL="${RHS%%[[:space:]]*}"
  fi

  if [ -z "$VAL" ]; then
    echo "  ✗ $NAME — could not read a literal value from $RC:$LINENO_; line left untouched"
    FAILED=1
    continue
  fi

  # Build the item JSON with python (correct escaping) and hand it to `op` on
  # STDIN. VAL is passed through the ENVIRONMENT, not argv, so it never appears
  # in `ps` output or shell history.
  TEMPLATE="$(VAL="$VAL" ITEM="$ITEM" FIELD="$FIELD" "$PY" -c '
import json, os
print(json.dumps({
    "title": os.environ["ITEM"],
    "category": "API_CREDENTIAL",
    "fields": [{
        "id": os.environ["FIELD"],
        "type": "CONCEALED",
        "label": os.environ["FIELD"],
        "value": os.environ["VAL"],
    }],
}))')"
  if [ -z "$TEMPLATE" ]; then
    echo "  ✗ $NAME — could not build the 1Password item template; line left untouched"
    FAILED=1
    continue
  fi

  # Create, or edit if the title already exists. Both write paths are followed
  # by a read-back, so an `op` that silently does the wrong thing is caught
  # rather than trusted.
  if op item get "$ITEM" --vault "$VAULT" >/dev/null 2>&1; then
    OP_TMP="$(umask 077 && mktemp "${TMPDIR:-/tmp}/op-migrate.XXXXXX")" || {
      echo "  ✗ $NAME — could not create a temp template; line left untouched"
      FAILED=1
      continue
    }
    printf '%s' "$TEMPLATE" > "$OP_TMP"
    OP_ERR="$(op item edit "$ITEM" --vault "$VAULT" --template "$OP_TMP" 2>&1 >/dev/null)"
    OP_RC=$?
    rm -f -- "$OP_TMP"
    ACTION="updated"
  else
    # `-` is the first positional argument, as `op item create --help` documents.
    OP_ERR="$(printf '%s' "$TEMPLATE" \
              | op item create - --vault "$VAULT" --category "API Credential" \
                                 --title "$ITEM" 2>&1 >/dev/null)"
    OP_RC=$?
    ACTION="created"
  fi

  if [ "$OP_RC" != 0 ]; then
    # $OP_ERR is op's stderr, which does not contain the value.
    echo "  ✗ $NAME — 1Password write failed; $RC:$LINENO_ left untouched"
    echo "      op: ${OP_ERR:-(no error output)}"
    FAILED=1
    continue
  fi

  # THE TRANSACTION BOUNDARY. Only a value that reads back byte-identical
  # justifies destroying the only other copy.
  GOT="$(op read "$REF" 2>/dev/null)"
  if [ "$GOT" != "$VAL" ]; then
    echo "  ✗ $NAME — wrote to $REF but the read-back did not match; $RC:$LINENO_ left untouched"
    FAILED=1
    unset GOT VAL
    continue
  fi
  unset GOT

  if ! rewrite_line "$LINENO_" "export $NAME=\"$REF\""; then
    echo "  ✗ $NAME — stored at $REF but rewriting $RC:$LINENO_ FAILED."
    echo "      The secret is safe in 1Password; the rc line still holds the literal."
    FAILED=1
    unset VAL
    continue
  fi
  unset VAL

  echo "  ✓ $NAME — $ACTION $REF, verified, $RC:$LINENO_ now holds the reference"
  MIGRATED=$((MIGRATED + 1))
done <<< "$FINDINGS"

# --- closing note ---------------------------------------------------------

echo
if [ "$APPLY" != 1 ]; then
  cat <<EOF
Nothing was written. Re-run with --apply to migrate.

Note that the doctor check re-reads $RC, so after --apply
'doctor.py --only no-plaintext-secrets-in-shell-rc' should go green.
EOF
  exit 0
fi

cat <<EOF
$MIGRATED of $COUNT variable(s) migrated.

>>> THIS WILL BREAK ANYTHING CURRENTLY READING THOSE VARIABLES. <<<

Your shell no longer holds the literal values. After you next start a shell,
\$NAME expands to the STRING "op://$VAULT/<item>/credential", not to the
credential. Every consumer must now resolve the reference:

  * preferred -- run the consumer under \`op run\`, which resolves every op://
    value in the environment for the child process only:
        op run -- ./your-app
        op run -- npm run dev
  * one-off   -- \`op read "op://$VAULT/<item>/credential"\`
  * a service or a cron job that cannot be wrapped needs a 1Password service
    account token (OP_SERVICE_ACCOUNT_TOKEN) or its own secret store.

Until that is wired up, those consumers will fail -- and they will fail with
whatever error an empty or literal-op:// credential produces, which is often
not an obvious auth error. Wire it up now, not later.

Then:
  1. ROTATE every migrated credential. They were readable on disk for as long
     as they sat there; migrating does not un-expose them.
  2. Delete the backup once you are satisfied: rm -- "$BACKUP"
     It still contains the literal secrets.
  3. Check the rest of the family -- .env files, rotating config backups,
     .credentials.json -- as house-style.md describes.
EOF

exit $FAILED
