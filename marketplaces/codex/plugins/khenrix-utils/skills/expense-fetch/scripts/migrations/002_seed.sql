-- 002_seed.sql — reference data (idempotent; safe to re-run).
-- People, the three card/bank accounts (all held by Christoffer), and the compact Swedish taxonomy.

-- ── people ────────────────────────────────────────────────────────────────────
insert into person (name) values
  ('Christoffer Henriksson'),
  ('Anna Knoph')
on conflict (name) do nothing;

-- ── accounts (holder = Christoffer for all three) ─────────────────────────────
insert into account (slug, provider, display_name, holder_id, currency)
select v.slug, v.provider, v.display_name, p.id, 'SEK'
from (values
  ('swedbank',      'swedbank', 'Swedbank'),
  ('amex-se',       'amex',     'American Express SE'),
  ('sas-eurobonus', 'sas',      'SAS EuroBonus World Mastercard')
) as v(slug, provider, display_name)
cross join (select id from person where name = 'Christoffer Henriksson') p
on conflict (slug) do nothing;

-- ── compact Swedish household taxonomy (~14 top-level categories) ──────────────
insert into category (slug, name) values
  ('dagligvaror',     'Dagligvaror'),          -- groceries
  ('restaurang',      'Restaurang & Café'),
  ('transport',       'Transport & Resor'),    -- public transit, taxi, parking
  ('drivmedel',       'Drivmedel'),            -- fuel
  ('boende',          'Boende & Hem'),         -- rent/mortgage, home goods
  ('el-internet',     'Hushållsel & Internet'),
  ('halsa',           'Hälsa & Apotek'),
  ('shopping',        'Shopping & Kläder'),
  ('noje',            'Nöje & Fritid'),
  ('systembolaget',   'Systembolaget'),
  ('prenumerationer', 'Prenumerationer'),      -- subscriptions
  ('avgifter',        'Avgifter & Ränta'),     -- fees & interest
  ('resa',            'Resa'),                 -- travel (flights, hotels)
  ('ovrigt',          'Övrigt')                -- other
on conflict (slug) do nothing;
