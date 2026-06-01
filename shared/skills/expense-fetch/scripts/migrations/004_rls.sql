-- 004_rls.sql — enable Row Level Security on every public table (deny-all baseline). Idempotent.
-- WHY: the DB is internet-facing over PostgREST and the anon/publishable key is public by design. With
-- RLS OFF, anyone holding that key can read/write all financial data. RLS ON with NO policies denies the
-- anon & authenticated roles everything. The local tool connects with the Supabase SECRET key
-- (service_role, which has BYPASSRLS), so it is unaffected. We do NOT `force` RLS, so the table owner
-- (the postgres role used for psql migrations) keeps working.
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;
