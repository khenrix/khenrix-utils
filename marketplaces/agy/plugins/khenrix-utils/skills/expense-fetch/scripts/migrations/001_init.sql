-- 001_init.sql — greenfield schema for the expense-fetch / expense-review skills.
-- Idempotent: safe to re-run. Target: Supabase Postgres (pg15+; gen_random_uuid() is core).
-- Money is stored as INTEGER MINOR UNITS (öre), never float/decimal. Currency is ISO-4217 CHAR(3).
-- Two independent lifecycle axes:
--   booking_status  pending|booked|reversed   (settlement lifecycle, owned by expense-fetch)
--   review_status   new|reviewed|ignored      (handling lifecycle, owned by expense-review)

-- ── reference / people ────────────────────────────────────────────────────────
create table if not exists person (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique,
  created_at  timestamptz not null default now()
);

create table if not exists account (
  id                   uuid primary key default gen_random_uuid(),
  slug                 text not null unique,                 -- swedbank | amex-se | sas-eurobonus
  provider             text not null,                        -- swedbank | amex | sas
  display_name         text not null,
  holder_id            uuid not null references person(id),  -- who pays this account (all = Christoffer)
  currency             char(3) not null default 'SEK',
  provider_account_ref text,                                 -- masked card/account id from the feed
  created_at           timestamptz not null default now()
);

create table if not exists category (
  id         uuid primary key default gen_random_uuid(),
  parent_id  uuid references category(id),
  name       text not null,
  slug       text not null unique,
  created_at timestamptz not null default now()
);

-- ── merchants: brand → location, with a provider-scoped descriptor cache ───────
create table if not exists merchant (
  id                   uuid primary key default gen_random_uuid(),
  canonical_name       text not null,
  slug                 text not null unique,
  default_category_id  uuid references category(id),
  website              text,
  logo_url             text,
  org_number           text,                                 -- Swedish organisationsnummer (optional)
  country              char(2) not null default 'SE',
  created_at           timestamptz not null default now()
);

create table if not exists merchant_location (
  id              uuid primary key default gen_random_uuid(),
  merchant_id     uuid not null references merchant(id) on delete cascade,
  name            text not null,                             -- e.g. "Coop Nära Finspång"
  city            text,
  address         text,
  lat             double precision,
  lng             double precision,
  google_place_id text,
  created_at      timestamptz not null default now()
);

-- local-first resolution cache: a normalized descriptor → merchant (+location), scoped per provider
-- (the same raw string can mean different things across Swedbank / Amex / SAS feeds).
create table if not exists descriptor_alias (
  id                    uuid primary key default gen_random_uuid(),
  provider              text not null,
  normalized_descriptor text not null,
  merchant_id           uuid not null references merchant(id) on delete cascade,
  merchant_location_id  uuid references merchant_location(id) on delete set null,
  confidence            numeric(4,3) not null default 1.0,
  created_at            timestamptz not null default now(),
  unique (provider, normalized_descriptor)
);

-- ── import staging / audit (thorough v1) ──────────────────────────────────────
create table if not exists import_run (
  id           uuid primary key default gen_random_uuid(),
  account_id   uuid not null references account(id),
  window_from  date,
  window_to    date,
  method       text not null,                                -- xhr_capture | export_csv | export_xlsx
  status       text not null default 'running',              -- running | ok | failed
  n_fetched    integer not null default 0,
  n_inserted   integer not null default 0,
  n_updated    integer not null default 0,
  n_skipped    integer not null default 0,
  n_flagged    integer not null default 0,                   -- possible-duplicate / needs-confirm
  notes        text,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz
);

-- ── transactions ──────────────────────────────────────────────────────────────
create table if not exists transaction (
  id                     uuid primary key default gen_random_uuid(),
  account_id             uuid not null references account(id),
  external_id            text,                                -- provider's stable id when present
  fingerprint            text not null,                       -- collision-safe dedup key (incl. same-day seq)

  booked_date            date,
  value_date             date,
  kind                   text not null default 'purchase',
  booking_status         text not null default 'booked',
  is_transfer            boolean not null default false,      -- internal transfer / reimbursement → off-balance

  charged_amount_minor   bigint not null,                     -- what hit the card (signed; expense negative)
  currency               char(3) not null default 'SEK',
  original_amount_minor  bigint,                              -- for FX purchases
  original_currency      char(3),
  fx_rate                numeric(18,8),

  raw_descriptor         text not null,
  normalized_descriptor  text,
  mcc                    text,                                -- merchant category code when the feed carries it

  merchant_id            uuid references merchant(id),
  merchant_location_id   uuid references merchant_location(id),
  category_id            uuid references category(id),

  shareable_amount_minor bigint,                              -- adjustable base; set in review, default=charged
  split_type             text not null default 'even',        -- even | exact | percent | personal

  review_status          text not null default 'new',
  reviewed_at            timestamptz,
  notes                  text,

  import_run_id          uuid references import_run(id),
  observed_first_at      timestamptz not null default now(),
  observed_last_at       timestamptz not null default now(),
  updated_at             timestamptz not null default now(),

  constraint transaction_kind_chk
    check (kind in ('purchase','refund','fee','interest','cash','transfer','payment','adjustment')),
  constraint transaction_booking_status_chk
    check (booking_status in ('pending','booked','reversed')),
  constraint transaction_review_status_chk
    check (review_status in ('new','reviewed','ignored')),
  constraint transaction_split_type_chk
    check (split_type in ('even','exact','percent','personal')),
  -- sign-aware: shareable must not exceed charged in magnitude, and share the sign (or be zero / personal).
  constraint transaction_shareable_sane_chk
    check (
      shareable_amount_minor is null
      or (abs(shareable_amount_minor) <= abs(charged_amount_minor)
          and (shareable_amount_minor = 0 or sign(shareable_amount_minor) = sign(charged_amount_minor)))
    )
);

-- raw observation rows kept for audit + idempotent reruns (links back to the normalized transaction).
create table if not exists raw_observation (
  id             uuid primary key default gen_random_uuid(),
  import_run_id  uuid not null references import_run(id) on delete cascade,
  account_id     uuid not null references account(id),
  provider_ref   text,                                        -- provider row id if any
  payload        jsonb not null,                              -- the raw captured/exported row
  status         text not null default 'raw',                 -- raw | normalized | duplicate | flagged | error
  transaction_id uuid references transaction(id) on delete set null,
  observed_at    timestamptz not null default now()
);

-- Dedup keys:
--   1) stable provider id when present (partial unique — Postgres allows many NULLs otherwise)
create unique index if not exists transaction_account_external_ux
  on transaction (account_id, external_id) where external_id is not null;
--   2) collision-safe fingerprint (the fingerprint itself encodes a same-day occurrence index,
--      so two identical 39 kr Pressbyrån buys on one day get distinct fingerprints and are both kept)
create unique index if not exists transaction_account_fingerprint_ux
  on transaction (account_id, fingerprint);

create index if not exists transaction_review_status_ix on transaction (review_status);
create index if not exists transaction_account_booking_ix on transaction (account_id, booking_status);
create index if not exists transaction_booked_date_ix on transaction (booked_date);

-- ── splits (materialized for EVERY transaction, incl. default 50/50) ───────────
-- Invariant (app-enforced; payer absorbs the öre remainder):
--   SUM(share_amount_minor) over a transaction = COALESCE(shareable_amount_minor, charged_amount_minor)
create table if not exists transaction_split (
  id                 uuid primary key default gen_random_uuid(),
  transaction_id     uuid not null references transaction(id) on delete cascade,
  person_id          uuid not null references person(id),
  share_amount_minor bigint not null,
  created_at         timestamptz not null default now(),
  unique (transaction_id, person_id)
);

-- ── settlements (fast-follow; table provisioned now) ──────────────────────────
create table if not exists settlement (
  id             uuid primary key default gen_random_uuid(),
  from_person_id uuid not null references person(id),
  to_person_id   uuid not null references person(id),
  amount_minor   bigint not null,
  currency       char(3) not null default 'SEK',
  date           date not null default current_date,
  note           text,
  created_at     timestamptz not null default now()
);

-- ── balance: how much each non-payer owes the account holder, per currency ─────
-- Excludes transfers/reimbursements and only counts reviewed rows. Settlements netting is fast-follow.
create or replace view v_balance as
  select ts.person_id      as debtor_id,
         a.holder_id        as creditor_id,
         t.currency,
         sum(ts.share_amount_minor) as owed_minor
  from transaction t
  join account a            on a.id = t.account_id
  join transaction_split ts on ts.transaction_id = t.id
  where t.is_transfer = false
    and t.review_status = 'reviewed'
    and ts.person_id <> a.holder_id
  group by ts.person_id, a.holder_id, t.currency;
