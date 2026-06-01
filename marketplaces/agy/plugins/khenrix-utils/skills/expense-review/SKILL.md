---
name: expense-review
description: >-
  Go through un-handled bank/card transactions in the Supabase expense database one at a time —
  resolve each to a canonical merchant, categorize it, and decide how the cost splits between
  Christoffer Henriksson and Anna Knoph (default 50/50 of an adjustable shareable amount; or mark it
  personal / ignored). Reads transactions that expense-fetch ingested (review_status='new'); needs no
  bank access. Use when the user wants to review, categorize, split, or "go through" their expenses,
  settle who owes whom, or fix a previously-reviewed transaction. Triggers: "go through my expenses",
  "review expenses", "categorize transactions", "split the costs", "what does Anna owe", "handle the
  new transactions".
allowed-tools: Bash, Read
---

# expense-review

Walk the **un-handled** transactions in Supabase one by one: resolve merchant → categorize → split →
write. No browser, no bank login — it only reads/writes the database that `expense-fetch` populates.
The engine (`scripts/review.py`, `db.py`, `enrich.py`, `normalize.py`) does the deterministic DB writes
and split math; **you** do the merchant/category/split reasoning and talk to the user.

## Setup (once per run)

- `cd` to this skill's `scripts/` directory so `import db, review, enrich, normalize` resolve.
- Secrets load from `~/.config/khenrix-utils/expenses.env` (or env): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
  (+ optional `GOOGLE_PLACES_API_KEY`). If a Supabase var is missing, stop and tell the user.
- Category meanings + merchant→category rules: read `../references/taxonomy.md`.
- **Never print secrets.** Money is integer **öre** everywhere (−10000 = −100.00 SEK; expenses are negative).

## Step 1 — pull the queue

Default scope is every `review_status='new'` row, oldest first (override with an account/date filter if asked):

```bash
python3 - <<'PY'
import db, review, json
d = db.PostgREST()
rows = review.pending(d, limit=200)
print(json.dumps([{k: r[k] for k in ("id","account_id","booked_date","charged_amount_minor",
      "currency","raw_descriptor","normalized_descriptor","mcc","merchant_id","booking_status")} for r in rows]))
PY
```

Tell the user how many are queued, then process them **one at a time** (or batch known vendors — see below).

## Step 2 — the per-transaction loop

For each transaction:

**a. Resolve the merchant (local-first).** `enrich.resolve_merchant(d, provider, raw_descriptor)` returns
`source: cache` (already known → auto-fill, no questions), or `source: places` with a `candidate`
(Google Places suggestion), or `source: none` (fall back to your own reasoning from the descriptor).
The `provider` is the account's provider (`swedbank`/`amex`/`sas`).

```bash
python3 - <<'PY'
import db, enrich, json
d = db.PostgREST()
print(json.dumps(enrich.resolve_merchant(d, "amex", "KLARNA *SYSTEMBOLAGET STHLM"), default=str))
PY
```

- **Same brand, new location** (e.g. Coop Stockholm vs Coop Finspång): reuse the existing `merchant`,
  add a new `merchant_location` under it — never a second Coop merchant.
- New merchant → confirm the canonical name with the user, then `review.upsert_merchant(...)`,
  optionally `review.upsert_location(...)`, and **always** `review.cache_alias(...)` so it's free next time.

**b. Categorize.** Order: cached merchant default → `mcc` (if present, map via taxonomy) → descriptor rules
in `taxonomy.md` → your best judgment. Propose one of the 14 category slugs; let the user correct.
Write the confirmed category onto the merchant (`default_category_id`) so repeats auto-fill.

**c. Decide the split.** Default is **even 50/50 of the shareable amount**, where `shareable_amount_minor`
defaults to `charged_amount_minor`. Ask only when it isn't the default. Cases:
  - *Normal shared expense* → `split_type='even'`, shareable = charged, `review.even_split(shareable, [cid,aid], payer)`.
  - *Christoffer fronted a group bill & was reimbursed* → set `shareable_minor` to the **truly shared part**
    (e.g. −80000 of a −200000 charge); split that 50/50; the remainder stays off the balance.
  - *Anna wasn't involved* → `split_type='personal'`, `shareable_minor=0`, `splits=[]` (whole charge is his).
  - *Custom* → `split_type='exact'|'percent'`, provide explicit `splits` that sum to `shareable_minor`.
  - Payer = the account holder (all three accounts = Christoffer). Payer absorbs the öre remainder.

**d. Commit.**

```bash
python3 - <<'PY'
import db, review
d = db.PostgREST()
cid, aid = db.person_id(d,"Christoffer Henriksson"), db.person_id(d,"Anna Knoph")
cat = review.category_id(d, "systembolaget")
TX = "<transaction-uuid>"; SHAREABLE = -23900            # from the row / user's adjustment
splits = review.even_split(SHAREABLE, [cid, aid], payer_id=cid)
review.commit(d, TX, shareable_minor=SHAREABLE, split_type="even", splits=splits,
              merchant_id="<merchant-uuid>", merchant_location_id=None, category_id=cat)
PY
```

## Ignored / transfers / refunds

Internal transfers (e.g. Swedbank → SAS card payment) and reimbursements are **not** spend to split —
`review.mark_ignored(d, TX, is_transfer=True, kind="transfer")`. They stay off `v_balance`, no category.
**Refunds/credits need care:** if it's a refund of a *shared* purchase, **split it symmetrically** (positive
`shareable_minor`, opposite sign of the original) so it credits Anna back — do NOT blanket-ignore it or she
keeps owing her half. Only `mark_ignored(..., kind="refund")` a refund of a *personal/ignored* purchase.

## Company expenses (Wint) & reimbursements

`expense-fetch` auto-links Wint receipts to bank rows. Handle the two ends:
- A bank charge with **`is_reimbursable=true`** (it mirrors a `wint_expense.charge_transaction_id`) is a
  *company* cost, not a household one → default it to **`split_type='personal'`, `shareable_minor=0`, no
  splits** (off the Christoffer↔Anna balance — the company pays it back). Still categorize it normally.
- A **reimbursement deposit** (`kind='reimbursement'`, `is_transfer=true`) is already off the balance —
  leave it `ignored`; don't split or categorize it as income.
- Show settlement state with `d.select("v_wint_reconciliation", {})`: `pending` (awaiting payout),
  `paid_unlinked` (Wint paid out but no matching Swedbank deposit found — help the user locate/confirm it),
  `settled` (matched). Summarize "company expenses still owed to you: N (X kr)".

## Batch mode (keep it fast)

Group the queue by resolved merchant. For vendors that hit `source: cache` with an unambiguous default
category and a plain even split, present them together ("12 known vendors → auto-apply even split?") and
commit in a loop on one confirmation. Only stop and ask on new merchants, odd amounts, or non-default splits.

## Corrections

To fix a mistake, `review.reopen(d, TX)` pulls a reviewed/ignored row back to `new` (clears its splits);
re-run the loop on it. "Undo last" = reopen the most recently `reviewed_at` row.

## Show the balance

```bash
python3 - <<'PY'
import db
d = db.PostgREST()
for r in d.select("v_net_balance", {}):   # settlement-aware (nets recorded payments); v_balance = gross
    print(r["debtor_id"], "owes", r["creditor_id"], r["net_owed_minor"]/100, r["currency"])
PY
```

Report **each currency separately** — never sum öre across currencies. Summarize per currency as "Anna owes
Christoffer X kr" (positive magnitude; the stored figure is negative). `v_net_balance` already nets any
`settlement` rows and excludes transfers, reimbursable charges, and unreviewed rows.

## Invariants (don't break)
- `sum(share_amount_minor) == shareable_amount_minor` (commit enforces it); `personal` ⇒ shareable 0, no rows.
- One canonical `merchant` per brand; locations hang off it; every raw descriptor caches into `descriptor_alias`.
- Re-running is safe — already-`reviewed` rows aren't in the queue.
