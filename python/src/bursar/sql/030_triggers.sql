CREATE FUNCTION bursar.reject_ledger_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO ''
AS $$
BEGIN
    RAISE EXCEPTION 'credit ledger entries are append-only';
END;
$$;

CREATE TRIGGER credit_ledger_entries_append_only
BEFORE UPDATE OR DELETE ON bursar.credit_ledger_entries
FOR EACH ROW
EXECUTE FUNCTION bursar.reject_ledger_mutation();

CREATE TRIGGER credit_accounts_updated_at
BEFORE UPDATE ON bursar.credit_accounts
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER credit_buckets_updated_at
BEFORE UPDATE ON bursar.credit_buckets
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER credit_leases_updated_at
BEFORE UPDATE ON bursar.credit_leases
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER account_plan_assignments_updated_at
BEFORE UPDATE ON bursar.account_plan_assignments
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER credit_spend_caps_updated_at
BEFORE UPDATE ON bursar.credit_spend_caps
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER credit_teams_updated_at
BEFORE UPDATE ON bursar.credit_teams
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_offers_updated_at
BEFORE UPDATE ON bursar.billing_offers
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_customers_updated_at
BEFORE UPDATE ON bursar.billing_customers
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_subscriptions_updated_at
BEFORE UPDATE ON bursar.billing_subscriptions
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_preferences_updated_at
BEFORE UPDATE ON bursar.billing_preferences
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_checkout_intents_updated_at
BEFORE UPDATE ON bursar.billing_checkout_intents
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_subscription_changes_updated_at
BEFORE UPDATE ON bursar.billing_subscription_changes
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_auto_recharge_profiles_updated_at
BEFORE UPDATE ON bursar.billing_auto_recharge_profiles
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();

CREATE TRIGGER billing_auto_recharge_attempts_updated_at
BEFORE UPDATE ON bursar.billing_auto_recharge_attempts
FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();
