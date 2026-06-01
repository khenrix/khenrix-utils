-- 007_settlement_netting.sql — net recorded settlements against the gross balance (idempotent).
-- v_balance is gross-lifetime; once Anna pays Christoffer back (a `settlement` row from Anna→Christoffer),
-- the amount owed should drop. owed_minor is negative (a debt); a settlement in the debtor→creditor
-- direction has a positive amount, so net = owed_minor + paid_minor moves the debt toward zero.
create or replace view v_net_balance as
  with paid as (
    select from_person_id as debtor_id, to_person_id as creditor_id, currency,
           sum(amount_minor) as paid_minor
    from settlement
    group by from_person_id, to_person_id, currency)
  select b.debtor_id, b.creditor_id, b.currency,
         b.owed_minor + coalesce(p.paid_minor, 0) as net_owed_minor
  from v_balance b
  left join paid p
    on p.debtor_id = b.debtor_id and p.creditor_id = b.creditor_id and p.currency = b.currency;
