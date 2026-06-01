-- 010_merchant_orders.sql — order-level deep-enrichment (Amazon / Google / PayPal line items). Idempotent.
-- One merchant order can map to SEVERAL card charges (Amazon ships + charges per shipment), so the order↔
-- transaction link is N:M. Order/line amounts are POSITIVE minor units (the amount billed); `transaction`
-- charges stay negative — the matcher compares magnitudes. Reference data: review still owns categorize/split;
-- a deep-enriched charge is committed with enrichment_source='merchant' (migration 009).

create table if not exists merchant_order (
  id                uuid primary key default gen_random_uuid(),
  source            text not null,                  -- amazon | google-play | google-pay | paypal | apple | klarna
  external_order_id text not null,                  -- the merchant's own order id
  merchant_id       uuid references merchant(id),   -- canonical merchant (set during review)
  order_date        date,
  total_minor       bigint,                         -- order total, POSITIVE minor units
  currency          char(3) not null default 'SEK',
  status            text,                           -- merchant order status (shipped / delivered / …)
  raw               jsonb,
  observed_at       timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (source, external_order_id)
);

create table if not exists merchant_order_line (
  id                uuid primary key default gen_random_uuid(),
  order_id          uuid not null references merchant_order(id) on delete cascade,
  line_seq          integer not null default 0,     -- stable position within the order (idempotent re-write)
  description       text not null,
  qty               numeric,
  unit_amount_minor bigint,
  amount_minor      bigint,                          -- line total, POSITIVE minor units
  currency          char(3) not null default 'SEK',
  category_hint     text,                            -- optional taxonomy slug guess from the item
  raw               jsonb,
  created_at        timestamptz not null default now(),
  unique (order_id, line_seq)
);

create table if not exists order_charge_link (
  id             uuid primary key default gen_random_uuid(),
  order_id       uuid not null references merchant_order(id) on delete cascade,
  transaction_id uuid not null references transaction(id) on delete cascade,
  amount_minor   bigint,                             -- portion of this charge attributed to the order
  created_at     timestamptz not null default now(),
  unique (order_id, transaction_id)
);

create index if not exists merchant_order_line_order_ix on merchant_order_line(order_id);
create index if not exists order_charge_link_tx_ix       on order_charge_link(transaction_id);
create index if not exists order_charge_link_order_ix    on order_charge_link(order_id);

-- "what did I actually buy" — a transaction joined to its order's line items.
create or replace view v_transaction_detail as
  select ocl.transaction_id, mo.id as order_id, mo.source, mo.external_order_id,
         mo.order_date, mo.total_minor as order_total_minor, mo.currency as order_currency, mo.status,
         mol.line_seq, mol.description, mol.qty, mol.amount_minor, mol.currency as line_currency
  from order_charge_link ocl
  join merchant_order mo       on mo.id = ocl.order_id
  left join merchant_order_line mol on mol.order_id = mo.id;

-- RLS, mirroring 004: deny-all to anon/authenticated; the local tool's SECRET key (service_role) bypasses.
alter table merchant_order       enable row level security;
alter table merchant_order_line  enable row level security;
alter table order_charge_link    enable row level security;
alter view  v_transaction_detail set (security_invoker = on);
revoke all on v_transaction_detail from anon, authenticated;
