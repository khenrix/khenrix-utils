-- 009_enrichment_provenance.sql — record HOW a transaction's merchant/category was derived (idempotent).
-- Lets review label each row's enrichment source + confidence so low-confidence auto-labels can be
-- re-reviewed, and so "captured details first" (source-provided merchant/MCC) is distinguishable from a
-- Google Places guess. Additive: new nullable columns + an extended review_commit (defaults keep old calls
-- working). No behavioural change to expense-fetch.

alter table transaction
  add column if not exists enrichment_source     text,
  add column if not exists enrichment_confidence numeric(4,3);

-- source ∈ how the merchant/category was resolved (NULL until reviewed):
--   cache   = descriptor_alias hit (already known)        manual = human typed it / overrode
--   details = merchant the SOURCE captured (Amex mn / extended_details.merchant; Swedbank details pass)
--   mcc     = category inferred from a captured MCC        places = Google Places candidate
--   wint    = identified from the linked Wint receipt (reimbursable company charge)
alter table transaction drop constraint if exists transaction_enrichment_source_chk;
alter table transaction add constraint transaction_enrichment_source_chk
  check (enrichment_source is null
         or enrichment_source in ('cache','details','mcc','places','wint','manual'));

alter table transaction drop constraint if exists transaction_enrichment_conf_chk;
alter table transaction add constraint transaction_enrichment_conf_chk
  check (enrichment_confidence is null
         or (enrichment_confidence >= 0 and enrichment_confidence <= 1));

create index if not exists transaction_enrichment_conf_ix
  on transaction (enrichment_confidence) where review_status = 'reviewed';

-- Extend the atomic finalize with provenance. Drop the old 7-arg signature first (a CREATE with a
-- different arg list would add an overload, not replace it → PostgREST ambiguity). The two new params
-- default to NULL, so a caller that omits them still resolves to this one function.
drop function if exists review_commit(uuid,bigint,text,uuid,uuid,uuid,jsonb);
create or replace function review_commit(
  p_tx uuid, p_shareable bigint, p_split_type text,
  p_merchant uuid, p_location uuid, p_category uuid, p_splits jsonb,
  p_enrich_source text default null, p_enrich_confidence numeric default null
) returns void
language plpgsql
as $$
begin
  if p_shareable = 0 and p_splits is not null and jsonb_array_length(p_splits) > 0 then
    raise exception 'zero shareable (personal) must have no split rows';
  end if;
  update transaction set
    merchant_id            = p_merchant,
    merchant_location_id   = p_location,
    category_id            = p_category,
    shareable_amount_minor = p_shareable,
    split_type             = p_split_type,
    enrichment_source      = p_enrich_source,
    enrichment_confidence  = p_enrich_confidence,
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

  if p_shareable <> 0 and coalesce(
       (select sum(share_amount_minor) from transaction_split where transaction_id = p_tx), 0) <> p_shareable then
    raise exception 'split rows (sum=%) do not equal shareable %',
      (select sum(share_amount_minor) from transaction_split where transaction_id = p_tx), p_shareable;
  end if;
end $$;
