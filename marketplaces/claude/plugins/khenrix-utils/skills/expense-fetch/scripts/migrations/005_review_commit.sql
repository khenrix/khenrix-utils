-- 005_review_commit.sql — atomic finalize for expense-review (idempotent: create or replace).
-- Replaces the 3 separate PostgREST calls (update tx → delete splits → insert splits) with ONE
-- transactional function, so a mid-sequence failure can never leave a row 'reviewed' with zero/partial
-- splits (which v_balance would silently count as 0 and never re-queue). Called via PostgREST /rpc/.
create or replace function review_commit(
  p_tx uuid,
  p_shareable bigint,
  p_split_type text,
  p_merchant uuid,
  p_location uuid,
  p_category uuid,
  p_splits jsonb
) returns void
language plpgsql
as $$
begin
  update transaction set
    merchant_id            = p_merchant,
    merchant_location_id   = p_location,
    category_id            = p_category,
    shareable_amount_minor = p_shareable,
    split_type             = p_split_type,
    review_status          = 'reviewed',
    reviewed_at            = now(),
    updated_at             = now()
  where id = p_tx;

  delete from transaction_split where transaction_id = p_tx;

  if p_splits is not null and jsonb_array_length(p_splits) > 0 then
    insert into transaction_split (transaction_id, person_id, share_amount_minor)
    select p_tx, (e->>'person_id')::uuid, (e->>'share_amount_minor')::bigint
    from jsonb_array_elements(p_splits) e;
  end if;

  -- guard inside the txn: a nonzero shareable must be fully allocated
  if p_shareable <> 0 and coalesce(
       (select sum(share_amount_minor) from transaction_split where transaction_id = p_tx), 0) <> p_shareable then
    raise exception 'split rows (sum=%) do not equal shareable %',
      (select sum(share_amount_minor) from transaction_split where transaction_id = p_tx), p_shareable;
  end if;
end $$;
