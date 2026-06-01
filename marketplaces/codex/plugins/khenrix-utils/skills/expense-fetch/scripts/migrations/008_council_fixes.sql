-- 008_council_fixes.sql — confirmation-council fixes (idempotent).

-- 1) VIEW RLS BYPASS (C1 was incomplete): a view runs with its owner's rights unless security_invoker,
--    so anon could read financial data through v_balance/v_wint_reconciliation/v_net_balance even with
--    table RLS on. Make views run as the querying role (so table RLS applies) AND revoke anon/authenticated.
alter view v_balance              set (security_invoker = on);
alter view v_wint_reconciliation  set (security_invoker = on);
alter view v_net_balance          set (security_invoker = on);
revoke all on v_balance, v_wint_reconciliation, v_net_balance from anon, authenticated;

-- 2) ATOMIC WINT RECONCILE (the H3/M2 fix had introduced a new non-atomic link→delete→flag sequence).
create or replace function reconcile_charge(p_wint uuid, p_tx uuid) returns void
language plpgsql as $$
begin
  delete from transaction_split where transaction_id = p_tx;       -- a 50/50 split must be redone as personal
  update transaction set is_reimbursable = true, review_status = 'new', reviewed_at = null,
                         updated_at = now()
    where id = p_tx;
  update wint_expense set charge_transaction_id = p_tx, updated_at = now() where id = p_wint;
end $$;

create or replace function reconcile_reimbursement(p_wint uuid, p_tx uuid) returns void
language plpgsql as $$
begin
  update transaction set kind = 'reimbursement', is_transfer = true, updated_at = now() where id = p_tx;
  update wint_expense set reimbursed_transaction_id = p_tx, updated_at = now() where id = p_wint;
end $$;

-- 3) ZERO-SHARE HOLE: a 'personal'/zero-shareable transaction must carry NO split rows (else a bad
--    zero-sum split could still credit/debit Anna). Re-define review_commit with that extra guard.
create or replace function review_commit(
  p_tx uuid, p_shareable bigint, p_split_type text,
  p_merchant uuid, p_location uuid, p_category uuid, p_splits jsonb
) returns void
language plpgsql as $$
begin
  if p_shareable = 0 and p_splits is not null and jsonb_array_length(p_splits) > 0 then
    raise exception 'zero shareable (personal) must have no split rows';
  end if;
  update transaction set
    merchant_id = p_merchant, merchant_location_id = p_location, category_id = p_category,
    shareable_amount_minor = p_shareable, split_type = p_split_type,
    review_status = 'reviewed', reviewed_at = now(), updated_at = now()
  where id = p_tx;
  delete from transaction_split where transaction_id = p_tx;
  if p_splits is not null and jsonb_array_length(p_splits) > 0 then
    insert into transaction_split (transaction_id, person_id, share_amount_minor)
    select p_tx, (e->>'person_id')::uuid, (e->>'share_amount_minor')::bigint
    from jsonb_array_elements(p_splits) e;
  end if;
  if p_shareable <> 0 and coalesce(
       (select sum(share_amount_minor) from transaction_split where transaction_id = p_tx), 0) <> p_shareable then
    raise exception 'split rows (sum=%) do not equal shareable %',
      (select sum(share_amount_minor) from transaction_split where transaction_id = p_tx), p_shareable;
  end if;
end $$;

-- 4) BIDIRECTIONAL settlement netting — a settlement Christoffer→Anna must also count (not just Anna→Christoffer).
create or replace view v_net_balance as
  with paid as (
    select from_person_id as debtor_id, to_person_id as creditor_id, currency,
           sum(amount_minor) as paid_minor
    from settlement group by from_person_id, to_person_id, currency)
  select b.debtor_id, b.creditor_id, b.currency,
         b.owed_minor + coalesce(p1.paid_minor, 0) - coalesce(p2.paid_minor, 0) as net_owed_minor
  from v_balance b
  left join paid p1 on p1.debtor_id = b.debtor_id and p1.creditor_id = b.creditor_id and p1.currency = b.currency
  left join paid p2 on p2.debtor_id = b.creditor_id and p2.creditor_id = b.debtor_id and p2.currency = b.currency;
alter view v_net_balance set (security_invoker = on);
revoke all on v_net_balance from anon, authenticated;
