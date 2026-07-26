REVOKE ALL ON SCHEMA bursar FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA bursar TO service_role;

REVOKE ALL ON ALL TABLES IN SCHEMA bursar FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA bursar FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA bursar TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bursar TO service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA bursar TO service_role;

REVOKE INSERT, UPDATE, DELETE ON bursar.credit_ledger_entries FROM service_role;
REVOKE UPDATE, DELETE ON bursar.credit_lot_allocations FROM service_role;
REVOKE UPDATE, DELETE ON bursar.credit_lot_reversals FROM service_role;
