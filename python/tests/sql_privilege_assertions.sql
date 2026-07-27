DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
END $$;
DO $$
BEGIN
  IF has_table_privilege('service_role','bursar.credit_ledger_entries','INSERT') THEN RAISE EXCEPTION 'service role can insert ledger directly'; END IF;
  IF has_function_privilege('service_role','bursar.require_internal_mutation()','EXECUTE') THEN RAISE EXCEPTION 'service role can execute internal trigger helper'; END IF;
    IF NOT has_function_privilege('service_role','bursar.post_credit(uuid,bursar.ledger_entry_kind,numeric,text,text,jsonb,text,uuid,timestamptz,numeric)','EXECUTE') THEN RAISE EXCEPTION 'service role cannot execute public posting routine'; END IF;
 IF NOT has_function_privilege('service_role','bursar.upsert_billing_customer(uuid,text,text,text)','EXECUTE') THEN RAISE EXCEPTION 'service role cannot execute billing write routine'; END IF;
 IF NOT has_function_privilege('service_role','bursar.get_billing_customer(uuid,text)','EXECUTE') THEN RAISE EXCEPTION 'service role cannot execute billing read routine'; END IF;
 IF has_function_privilege('service_role','bursar.bucket_expiry_at(uuid,uuid,text)','EXECUTE') THEN RAISE EXCEPTION 'service role can execute internal expiry helper'; END IF;
END $$;
