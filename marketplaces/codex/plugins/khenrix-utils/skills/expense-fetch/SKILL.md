---
name: expense-fetch
description: >-
  Pull bank/card transactions and Wint company receipts into the Supabase expense database, deduped and
  staged for review. Drives the logged-in browser via the chrome-devtools MCP to capture each source's
  own API responses (Amex SE, Swedbank, SAS EuroBonus, Wint), normalizes + dedupes them, and inserts them
  with review_status='new' for the expense-review skill to handle. Default window is "since the latest
  stored transaction" per account. Use when the user wants to fetch, import, sync, or download new
  expenses/transactions/receipts, or "get my latest expenses". Auth (BankID / OTP / Wint login) is
  human-in-the-loop. Triggers: "fetch my expenses", "import transactions", "sync Swedbank/Amex/SAS/Wint",
  "get new receipts", "update the expense database".
allowed-tools: Bash, Read, mcp__chrome-devtools__list_pages, mcp__chrome-devtools__new_page, mcp__chrome-devtools__select_page, mcp__chrome-devtools__navigate_page, mcp__chrome-devtools__take_snapshot, mcp__chrome-devtools__click, mcp__chrome-devtools__evaluate_script, mcp__chrome-devtools__list_network_requests, mcp__chrome-devtools__get_network_request
---

# expense-fetch

Capture transactions from four sources into Supabase as `review_status='new'` rows, then hand off to
`expense-review`. **Auth is human-in-the-loop** — the user logs in (BankID / password+OTP / Wint), you
drive the *already-authenticated* session. **Capture the app's own API responses.** A *bare* replay 400/401s
on every bank (the app injects auth headers the page can't reconstruct); you may replay only by reusing the
**exact headers captured from a real request** — that's how Swedbank's details pass and Wint's pagination
work. Never hardcode endpoint URLs as a durable contract; discover them live. Engine: `scripts/fetch.py` +
`db.py` + `normalize.py` (deterministic, tested).

## Setup
- `cd scripts/`; secrets from `~/.config/khenrix-utils/expenses.env` or env (`SUPABASE_URL`, `EXPENSES_SUPABASE_SECRET_KEY` — falls back to `SUPABASE_SECRET_KEY`).
- Apply migrations once if the DB is empty: `psql "$POSTGRES_URL_NON_POOLING" -f migrations/001_init.sql` (then `002`,`003`).
- Confirm the chrome-devtools MCP is connected (`list_pages`). Money is integer **öre**; expenses negative.
- **Default window** per account = `db.get_last_tx_date(d, slug)` (fetch only newer); also accept "last week", a range, or "all".
- **Id-less feeds need an overlap.** For a source with no stable per-row id and no running balance (SAS export
  if its sheet lacks a balance column), set the window to **~7 days *before* the latest stored date**, not
  exactly "since latest" — the occurrence-based dedup is only correct when a same-day duplicate group arrives
  *complete* in one batch (it cannot disambiguate a lone identical row in a partial fetch). Feeds with
  `external_id` (Swedbank `details/{id}`, Amex, Wint) or a `source_seq` running balance are immune.

## Capture recipes (per provider)

Open each `import_run` with `fetch.start_import(...)`, capture rows, `fetch.ingest_batch(...)`, then
`fetch.finish_import(...)`. Each adapter must produce **normalized row dicts**:
`{external_id?, booked_date, value_date, charged_amount_minor, currency, original_amount_minor?, original_currency?, fx_rate?, raw_descriptor, normalized_descriptor, kind?, booking_status, mcc?, raw_payload}`
(use `normalize.parse_descriptor(raw)["normalized"]` for `normalized_descriptor`).

### Amex SE  — clean REST, full detail inline (no second pass needed)
1. Ensure logged in (`americanexpress.com/sv-se` → `global.americanexpress.com`).
2. `navigate_page` → `https://global.americanexpress.com/activity`.
3. `list_network_requests(xhr,fetch)` → find both
   `…/api/servicing/v1/financials/transactions?status=posted&…extended_details=merchant,category,tags,rewards,offer,deferred_details,receipts,flags,plan_details,transaction_codes`
   and the `status=pending` twin. `get_network_request` each → JSON `transactions` array (merchant+category inline).
4. Map each: amount→öre, `status=pending`→`booking_status='pending'`, merchant/category/MCC from the inline detail.

### Swedbank — two-pass (list → per-transaction details), capture the app's calls
1. Logged in (BankID) → open an account's **Kontohistorik**.
2. List: capture `apionline.swedbank.se/TDE_DAP_Portal_REST_WEB/api/v5/engagement/transactions/{accountId}`
   (accounts from `…/engagement/overview`). Each row carries a detail id.
3. **Details (gather ALL detail):** the list/export omits merchant/MCC/message — for each row the app fetches
   `…/engagement/transactions/details/{detailId}` when opened. Capture those (open rows / or replay with the
   session's captured auth header read off a real request's headers) and merge. Store the full merged payload
   in `raw_payload`. Tolerate the new "Andromeda" shape if present.
4. Export fallback (if API blocked): **Konton→Översikt→Exportera** CSV(semicolon)/XLSX from `/mnt/c/…/Downloads`.

### SAS EuroBonus / SEB Kort — export only (no JSON API; WAF-protected)
1. Logged in at `my.saseurobonusmastercard.se` (BankID; never script `secure.sebkort.com` — WAF).
2. **Kontoutdrag → Exportera till Excel** per month; read the `.xlsx` from `/mnt/c/Users/…/Downloads`
   and parse with `fetch.parse_xlsx(path)`. Detail is thin → rely on `expense-review` enrichment.

### Wint — REST API, replay works (the only one)
1. Logged in at `app.wint.se`; capture one `api.wint.se` request's headers (`get_network_request`) to read the
   **`authorization: Bearer …`** (short-lived ~15 min) and **`companyid`** headers.
2. Replay, promptly, via `evaluate_script` from the `app.wint.se` page (CORS allows it) with those headers:
   `GET https://api.wint.se/api/Receipt?numPerPage=100&page=0..N&orderByProperty=DateTime&orderByDescending=true`
   (paginate all), plus `api/Receipt/{id}` for full detail/images.
3. `fetch.upsert_wint({...})` mapping: `wint_id=Id, serial_number=SerialNumber, receipt_date=DateTime,
   amount_minor=Amount, currency, amount_sek_minor=TotalAmountSummarySEK, supplier=SupplierName,
   category=CategoryName, payment_method=PaymentMethodName, paid_out=PaidOut, payment_state=PaymentState,
   payment_date=PaymentDate, recipient_account=RecipientAccount, receipt_url=<Images>, raw=<full payload>`.
4. After ingesting banks **and** Wint, run `fetch.reconcile_wint(d)` to link each receipt to its bank charge
   (`is_reimbursable=true`) and its reimbursement deposit (`kind='reimbursement'`, `is_transfer=true`).

## Ingest, dedup, staging
`fetch.ingest_batch(d, account, rows, import_run_id)` handles it all: collision-safe fingerprint (two
identical same-day buys are both kept), idempotent re-runs, and pending→booked promotion (un-reviewed rows
only). **Set `external_id` whenever the source gives a stable per-row id** (Swedbank `details/{id}` id, Amex
servicing id, Wint `Receipt.Id`) — it is the durable dedup key and the *only* reliable pending→booked path.
For the id-less **SAS export**, pass the row's **running balance** as `source_seq` so identical same-day rows
stay distinct without depending on fetch order; re-fetch the *full* window (not a partial slice) for
guaranteed idempotency on id-less feeds. Full raw payloads (insert/promote/duplicate) land in
`raw_observation`. Report a **reconciliation summary** at the end
(`{fetched, inserted, promoted, duplicate}` per account + Wint `reconcile_wint` counts), and **never silently
drop** an ambiguous row — flag it for the user.

## Driver template
```bash
python3 - <<'PY'
import db, fetch, normalize
d = db.PostgREST(); acct = db.account_by_slug(d, "amex-se")
run = fetch.start_import(d, acct["id"], method="xhr_capture")
rows = [ ... ]   # built from the captured JSON, normalized
counts = fetch.ingest_batch(d, acct, rows, import_run_id=run)
fetch.finish_import(d, run, counts); print(counts)
PY
```

## Hand-off
Everything lands as `review_status='new'`. Tell the user the per-source counts and that **`expense-review`**
will categorize + split them. Don't categorize or split here — that's the review skill's job.
