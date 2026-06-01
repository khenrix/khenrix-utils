-- 006_balance_fixes.sql — keep reimbursable company charges off the household balance (idempotent).
-- v_balance previously excluded only is_transfer; a Wint-linked company charge (is_reimbursable=true)
-- would otherwise still show Anna owing half of a cost the company pays back.
create or replace view v_balance as
  select ts.person_id as debtor_id,
         a.holder_id   as creditor_id,
         t.currency,
         sum(ts.share_amount_minor) as owed_minor
  from transaction t
  join account a            on a.id = t.account_id
  join transaction_split ts on ts.transaction_id = t.id
  where t.is_transfer = false
    and t.is_reimbursable = false
    and t.review_status = 'reviewed'
    and ts.person_id <> a.holder_id
  group by ts.person_id, a.holder_id, t.currency;
