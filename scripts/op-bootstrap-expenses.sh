#!/usr/bin/env bash
# Move the expenses secrets into a 1Password vault a SERVICE ACCOUNT can read, then print the
# op:// references to use in their place. Values move file -> 1Password over stdin and are never
# echoed, never placed on a command line (argv is world-readable via `ps`), and never returned.
#
# Service accounts CANNOT be granted the Personal or Private vault, which is why this creates a
# dedicated one rather than using the `op://Private/...` refs the house style currently documents.
#
# Requires an active session:  eval $(op signin) && bash scripts/op-bootstrap-expenses.sh
set -euo pipefail

VAULT="${OP_BOOTSTRAP_VAULT:-Automation}"
ITEM="expenses"
ENV_FILE="${HOME}/.config/khenrix-utils/expenses.env"
FRONTEND_ENV="${HOME}/git/expenses/frontend/.env.local"

command -v op >/dev/null || { echo "op CLI not found" >&2; exit 1; }
op vault list >/dev/null 2>&1 || {
  echo "not signed in — run:  eval \$(op signin) && bash $0" >&2; exit 1; }

# Read one KEY=VALUE out of an env file without echoing the value.
read_key() {  # read_key <file> <key>
  [ -f "$1" ] || return 1
  sed -n "s/^$2=//p" "$1" | head -1 | sed 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
}

if ! op vault get "$VAULT" >/dev/null 2>&1; then
  op vault create "$VAULT" >/dev/null
  echo "created vault: $VAULT"
else
  echo "vault exists:  $VAULT"
fi

# Build the field assignments in an array so values never appear in a rendered command string.
declare -a FIELDS=()
add_field() {  # add_field <label> <file> <key>
  local value
  value="$(read_key "$2" "$3" || true)"
  if [ -n "$value" ]; then
    FIELDS+=("$1[password]=$value")
    echo "  staged: $1"
  else
    echo "  MISSING (skipped): $1  — not found in $2"
  fi
}

echo "staging fields (values not shown):"
add_field supabase_secret_key      "$ENV_FILE"      SUPABASE_SECRET_KEY
add_field google_places_api_key    "$ENV_FILE"      GOOGLE_PLACES_API_KEY
add_field postgres_url             "$FRONTEND_ENV"  POSTGRES_URL
add_field postgres_url_non_pooling "$FRONTEND_ENV"  POSTGRES_URL_NON_POOLING

[ ${#FIELDS[@]} -gt 0 ] || { echo "nothing to store" >&2; exit 1; }

if op item get "$ITEM" --vault "$VAULT" >/dev/null 2>&1; then
  op item edit "$ITEM" --vault "$VAULT" "${FIELDS[@]}" >/dev/null
  echo "updated item:  $VAULT/$ITEM"
else
  op item create --category "API Credential" --vault "$VAULT" --title "$ITEM" \
    "${FIELDS[@]}" >/dev/null
  echo "created item:  $VAULT/$ITEM"
fi

cat <<REF

References to use in place of the literals (these are NOT secrets — they are pointers):

  SUPABASE_SECRET_KEY="op://$VAULT/$ITEM/supabase_secret_key"
  GOOGLE_PLACES_API_KEY="op://$VAULT/$ITEM/google_places_api_key"
  POSTGRES_URL="op://$VAULT/$ITEM/postgres_url"
  POSTGRES_URL_NON_POOLING="op://$VAULT/$ITEM/postgres_url_non_pooling"

Next, create the service account scoped to READ this vault only:

  op service-account create expenses-agent --vault "$VAULT:read_items"

It prints the token ONCE. Store it at 0600 and export it from there — never inline in a
shell rc, and never in a repo:

  install -m 600 /dev/null ~/.config/khenrix-utils/op-service-account
  # paste the token into that file, then in your shell rc:
  #   export OP_SERVICE_ACCOUNT_TOKEN="\$(cat ~/.config/khenrix-utils/op-service-account)"

Then consumers run under \`op run\`, which resolves op:// refs into the child's environment
without the values touching disk or reaching an agent:

  op run --env-file ~/.config/khenrix-utils/expenses.env -- <command>
REF
