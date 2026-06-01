-- 003_wint.sql — Wint company-expense source + reimbursement reconciliation (idempotent).
-- A Wint "receipt" is a company cost Christoffer fronted personally (PaymentMethod 'Eget utlägg') and is
-- reimbursed for; the reimbursement lands as a deposit in a bank account. We link both ends.

-- 1) Allow 'reimbursement' as a transaction kind (the incoming company payout).
alter table transaction drop constraint if exists transaction_kind_chk;
alter table transaction add constraint transaction_kind_chk
  check (kind in ('purchase','refund','fee','interest','cash','transfer','payment','adjustment','reimbursement'));

-- 2) Flag a bank charge that is a reimbursable company expense (mirrors a wint_expense link).
alter table transaction add column if not exists is_reimbursable boolean not null default false;

-- 3) The Wint receipt (mirrors api.wint.se /api/Receipt fields; full payload kept in `raw`).
create table if not exists wint_expense (
  id                        uuid primary key default gen_random_uuid(),
  wint_id                   text not null unique,            -- Receipt.Id
  serial_number             integer,                         -- Receipt.SerialNumber (e.g. 267)
  receipt_date              date,                            -- Receipt.DateTime
  amount_minor              bigint,                          -- Receipt.Amount (in its own currency)
  currency                  char(3),
  amount_sek_minor          bigint,                          -- Receipt.TotalAmountSummarySEK
  supplier                  text,                            -- Receipt.SupplierName
  category                  text,                            -- Receipt.CategoryName
  payment_method            text,                            -- Receipt.PaymentMethodName ('Eget utlägg')
  comment                   text,                            -- Receipt.Comment
  state                     text,                            -- Receipt.State (e.g. Bokförd)
  payment_state             text,                            -- Receipt.PaymentState (Betalstatus)
  paid_out                  boolean not null default false,  -- Receipt.PaidOut (reimbursed?)
  payment_date              date,                            -- Receipt.PaymentDate (reimbursed on)
  recipient_account         text,                            -- Receipt.RecipientAccount (paid to)
  recipient_clearing        text,                            -- Receipt.RecipientClearingNumber
  receipt_url               text,                            -- Receipt.Images / Attachments
  raw                       jsonb,                           -- complete Receipt payload — never lose detail
  charge_transaction_id     uuid references transaction(id), -- the bank charge it came from
  reimbursed_transaction_id uuid references transaction(id), -- the deposit that paid it back
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);
create index if not exists wint_expense_paid_out_ix on wint_expense (paid_out);
create index if not exists wint_expense_charge_ix    on wint_expense (charge_transaction_id);
create index if not exists wint_expense_reimb_ix     on wint_expense (reimbursed_transaction_id);
create index if not exists wint_expense_date_ix      on wint_expense (receipt_date);

-- 4) Reconciliation view: each company expense + whether it's charge-linked / reimbursed.
create or replace view v_wint_reconciliation as
  select w.id, w.serial_number, w.receipt_date, w.supplier, w.category, w.currency,
         w.amount_sek_minor, w.paid_out, w.payment_date,
         (w.charge_transaction_id     is not null) as charge_linked,
         (w.reimbursed_transaction_id is not null) as reimbursement_linked,
         case
           when w.reimbursed_transaction_id is not null then 'settled'       -- matched to a deposit
           when w.paid_out                              then 'paid_unlinked' -- Wint says paid, deposit not matched
           else 'pending'                                                    -- awaiting reimbursement
         end as recon_status
  from wint_expense w;
