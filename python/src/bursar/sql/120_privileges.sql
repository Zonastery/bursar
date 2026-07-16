-- Name: FUNCTION _upsert_billing_provider_ref(p_resource_type text, p_provider text, p_price_id text, p_product_id text, p_variant_id text, p_lookup_key text, p_resource_key text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar._upsert_billing_provider_ref(p_resource_type text, p_provider text, p_price_id text, p_product_id text, p_variant_id text, p_lookup_key text, p_resource_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar._upsert_billing_provider_ref(p_resource_type text, p_provider text, p_price_id text, p_product_id text, p_variant_id text, p_lookup_key text, p_resource_key text) TO service_role;


--
-- Name: FUNCTION _walk_and_debit_buckets(p_user_id uuid, p_amount numeric); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar._walk_and_debit_buckets(p_user_id uuid, p_amount numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar._walk_and_debit_buckets(p_user_id uuid, p_amount numeric) TO service_role;


--
-- Name: FUNCTION activate_bursar_config(p_version integer); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.activate_bursar_config(p_version integer) FROM PUBLIC;


--
-- Name: FUNCTION add_team_member(p_team_id uuid, p_user_id uuid, p_role text, p_spend_cap numeric); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.add_team_member(p_team_id uuid, p_user_id uuid, p_role text, p_spend_cap numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.add_team_member(p_team_id uuid, p_user_id uuid, p_role text, p_spend_cap numeric) TO service_role;


--
-- Name: FUNCTION aggregate_stats(p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.aggregate_stats(p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.aggregate_stats(p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION allocate_ledger_entry_lots(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.allocate_ledger_entry_lots() FROM PUBLIC;


--
-- Name: FUNCTION capture_activated_catalog_snapshot(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.capture_activated_catalog_snapshot() FROM PUBLIC;


--
-- Name: FUNCTION check_balance_invariant(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.check_balance_invariant(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.check_balance_invariant(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION check_feature_limit(p_user_id uuid, p_feature text, p_max_calls integer, p_period_start date, p_period_end date); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.check_feature_limit(p_user_id uuid, p_feature text, p_max_calls integer, p_period_start date, p_period_end date) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.check_feature_limit(p_user_id uuid, p_feature text, p_max_calls integer, p_period_start date, p_period_end date) TO service_role;


--
-- Name: FUNCTION check_plan_allowance(p_user_id uuid, p_period_start date); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.check_plan_allowance(p_user_id uuid, p_period_start date) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.check_plan_allowance(p_user_id uuid, p_period_start date) TO service_role;


--
-- Name: FUNCTION check_spend_cap(p_user_id uuid, p_model text, p_amount numeric); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.check_spend_cap(p_user_id uuid, p_model text, p_amount numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.check_spend_cap(p_user_id uuid, p_model text, p_amount numeric) TO service_role;


--
-- Name: FUNCTION claim_billing_event(p_provider text, p_event_id text, p_event_type text, p_envelope jsonb, p_lease_seconds integer, p_attempt_limit integer); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.claim_billing_event(p_provider text, p_event_id text, p_event_type text, p_envelope jsonb, p_lease_seconds integer, p_attempt_limit integer) FROM PUBLIC;


--
-- Name: FUNCTION complete_billing_event(p_provider text, p_event_id text, p_claim_token uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.complete_billing_event(p_provider text, p_event_id text, p_claim_token uuid) FROM PUBLIC;


--
-- Name: FUNCTION create_lease(p_user_id uuid, p_amount numeric, p_operation_type text, p_billing_mode text, p_floor numeric, p_max_concurrent integer, p_ttl_seconds integer, p_model text, p_overdraft_floor numeric, p_metadata jsonb, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.create_lease(p_user_id uuid, p_amount numeric, p_operation_type text, p_billing_mode text, p_floor numeric, p_max_concurrent integer, p_ttl_seconds integer, p_model text, p_overdraft_floor numeric, p_metadata jsonb, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.create_lease(p_user_id uuid, p_amount numeric, p_operation_type text, p_billing_mode text, p_floor numeric, p_max_concurrent integer, p_ttl_seconds integer, p_model text, p_overdraft_floor numeric, p_metadata jsonb, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date) TO service_role;


--
-- Name: FUNCTION create_team(p_name text, p_initial_balance numeric); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.create_team(p_name text, p_initial_balance numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.create_team(p_name text, p_initial_balance numeric) TO service_role;


--
-- Name: FUNCTION credits_add(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type, p_metadata jsonb, p_bucket text, p_idempotency_key text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.credits_add(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type, p_metadata jsonb, p_bucket text, p_idempotency_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.credits_add(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type, p_metadata jsonb, p_bucket text, p_idempotency_key text) TO service_role;


--
-- Name: FUNCTION credits_add_internal(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type, p_metadata jsonb, p_bucket text, p_idempotency_key text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.credits_add_internal(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type, p_metadata jsonb, p_bucket text, p_idempotency_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.credits_add_internal(p_user_id uuid, p_amount numeric, p_type bursar.credit_tx_type, p_metadata jsonb, p_bucket text, p_idempotency_key text) TO service_role;


--
-- Name: FUNCTION daily_spend(p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.daily_spend(p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.daily_spend(p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION deactivate_other_provider_subscriptions(p_user_id uuid, p_keep_provider text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.deactivate_other_provider_subscriptions(p_user_id uuid, p_keep_provider text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.deactivate_other_provider_subscriptions(p_user_id uuid, p_keep_provider text) TO service_role;


--
-- Name: FUNCTION deduct_team(p_team_id uuid, p_user_id uuid, p_amount numeric, p_metadata jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.deduct_team(p_team_id uuid, p_user_id uuid, p_amount numeric, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.deduct_team(p_team_id uuid, p_user_id uuid, p_amount numeric, p_metadata jsonb) TO service_role;


--
-- Name: FUNCTION deduct_with_allowance(p_user_id uuid, p_amount numeric, p_idempotency_key text, p_min_balance numeric, p_model text, p_metadata jsonb, p_skip_allowance boolean, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.deduct_with_allowance(p_user_id uuid, p_amount numeric, p_idempotency_key text, p_min_balance numeric, p_model text, p_metadata jsonb, p_skip_allowance boolean, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.deduct_with_allowance(p_user_id uuid, p_amount numeric, p_idempotency_key text, p_min_balance numeric, p_model text, p_metadata jsonb, p_skip_allowance boolean, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date) TO service_role;


--
-- Name: FUNCTION expire_credits(p_dry_run boolean, p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.expire_credits(p_dry_run boolean, p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.expire_credits(p_dry_run boolean, p_user_id uuid) TO service_role;


--
-- Name: FUNCTION expire_due_leases(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.expire_due_leases() FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.expire_due_leases() TO service_role;


--
-- Name: FUNCTION fail_billing_event(p_provider text, p_event_id text, p_claim_token uuid, p_error text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.fail_billing_event(p_provider text, p_event_id text, p_claim_token uuid, p_error text) FROM PUBLIC;


--
-- Name: FUNCTION get_active_bursar_config(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_active_bursar_config() FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_active_bursar_config() TO service_role;


--
-- Name: FUNCTION get_available_credits(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_available_credits(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_available_credits(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION get_billing_customer(p_provider text, p_provider_customer_id text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_billing_customer(p_provider text, p_provider_customer_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_billing_customer(p_provider text, p_provider_customer_id text) TO service_role;


--
-- Name: FUNCTION get_billing_customer_by_user_id(p_user_id uuid, p_provider text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_billing_customer_by_user_id(p_user_id uuid, p_provider text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_billing_customer_by_user_id(p_user_id uuid, p_provider text) TO service_role;


--
-- Name: FUNCTION get_billing_payment_for_refund(p_provider text, p_provider_payment_id text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_billing_payment_for_refund(p_provider text, p_provider_payment_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_billing_payment_for_refund(p_provider text, p_provider_payment_id text) TO service_role;


--
-- Name: FUNCTION get_billing_preferences(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_billing_preferences(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_billing_preferences(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION get_billing_subscription(p_provider text, p_provider_subscription_id text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_billing_subscription(p_provider text, p_provider_subscription_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_billing_subscription(p_provider text, p_provider_subscription_id text) TO service_role;


--
-- Name: FUNCTION get_bursar_config(p_version integer); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_bursar_config(p_version integer) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_bursar_config(p_version integer) TO service_role;


--
-- Name: FUNCTION get_bursar_configs(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_bursar_configs() FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_bursar_configs() TO service_role;


--
-- Name: FUNCTION get_credits_balance(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_credits_balance(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_credits_balance(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION get_team_balance(p_team_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_team_balance(p_team_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_team_balance(p_team_id uuid) TO service_role;


--
-- Name: FUNCTION get_team_members(p_team_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_team_members(p_team_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_team_members(p_team_id uuid) TO service_role;


--
-- Name: FUNCTION get_user_billing_subscription(p_user_id uuid, p_provider text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_user_billing_subscription(p_user_id uuid, p_provider text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_user_billing_subscription(p_user_id uuid, p_provider text) TO service_role;


--
-- Name: FUNCTION get_user_billing_subscriptions(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_user_billing_subscriptions(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_user_billing_subscriptions(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION get_user_credit_buckets(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_user_credit_buckets(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.get_user_credit_buckets(p_user_id uuid) TO service_role;


--
-- Name: FUNCTION get_user_plan(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.get_user_plan(p_user_id uuid) FROM PUBLIC;


--
-- Name: FUNCTION grant_signup_bonus(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.grant_signup_bonus() FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.grant_signup_bonus() TO service_role;


--
-- Name: FUNCTION increment_usage_window(p_user_id uuid, p_plan_id uuid, p_amount numeric, p_period_start date); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.increment_usage_window(p_user_id uuid, p_plan_id uuid, p_amount numeric, p_period_start date) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.increment_usage_window(p_user_id uuid, p_plan_id uuid, p_amount numeric, p_period_start date) TO service_role;


--
-- Name: FUNCTION list_transactions_cursor(p_user_id uuid, p_types text[], p_from_date timestamp with time zone, p_to_date timestamp with time zone, p_limit integer, p_cursor_created_at timestamp with time zone, p_cursor_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.list_transactions_cursor(p_user_id uuid, p_types text[], p_from_date timestamp with time zone, p_to_date timestamp with time zone, p_limit integer, p_cursor_created_at timestamp with time zone, p_cursor_id uuid) FROM PUBLIC;


--
-- Name: FUNCTION prevent_bursar_config_payload_mutation(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.prevent_bursar_config_payload_mutation() FROM PUBLIC;


--
-- Name: FUNCTION project_credit_transaction(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.project_credit_transaction() FROM PUBLIC;


--
-- Name: FUNCTION pseudonymize_financial_subject(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.pseudonymize_financial_subject(p_user_id uuid) FROM PUBLIC;


--
-- Name: FUNCTION publish_bursar_config(p_config jsonb, p_label text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.publish_bursar_config(p_config jsonb, p_label text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.publish_bursar_config(p_config jsonb, p_label text) TO service_role;


--
-- Name: FUNCTION reclaim_billing_event(p_provider text, p_event_id text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.reclaim_billing_event(p_provider text, p_event_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.reclaim_billing_event(p_provider text, p_event_id text) TO service_role;


--
-- Name: FUNCTION reconcile_credit_account(p_account_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.reconcile_credit_account(p_account_id uuid) FROM PUBLIC;


--
-- Name: FUNCTION record_refund_lot_provenance(); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.record_refund_lot_provenance() FROM PUBLIC;


--
-- Name: FUNCTION refund_credits(p_transaction_id uuid, p_amount numeric, p_reason text, p_metadata jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.refund_credits(p_transaction_id uuid, p_amount numeric, p_reason text, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.refund_credits(p_transaction_id uuid, p_amount numeric, p_reason text, p_metadata jsonb) TO service_role;


--
-- Name: FUNCTION release_lease(p_user_id uuid, p_lease_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.release_lease(p_user_id uuid, p_lease_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.release_lease(p_user_id uuid, p_lease_id uuid) TO service_role;


--
-- Name: FUNCTION renew_lease(p_user_id uuid, p_lease_id uuid, p_ttl_seconds integer); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.renew_lease(p_user_id uuid, p_lease_id uuid, p_ttl_seconds integer) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.renew_lease(p_user_id uuid, p_lease_id uuid, p_ttl_seconds integer) TO service_role;


--
-- Name: FUNCTION resolve_billing_offer_by_lookup(p_provider text, p_lookup_key text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.resolve_billing_offer_by_lookup(p_provider text, p_lookup_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.resolve_billing_offer_by_lookup(p_provider text, p_lookup_key text) TO service_role;


--
-- Name: FUNCTION resolve_billing_offer_by_price(p_provider text, p_price_id text, p_product_id text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.resolve_billing_offer_by_price(p_provider text, p_price_id text, p_product_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.resolve_billing_offer_by_price(p_provider text, p_price_id text, p_product_id text) TO service_role;


--
-- Name: FUNCTION resolve_credit_topup_by_lookup(p_provider text, p_lookup_key text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.resolve_credit_topup_by_lookup(p_provider text, p_lookup_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.resolve_credit_topup_by_lookup(p_provider text, p_lookup_key text) TO service_role;


--
-- Name: FUNCTION resolve_credit_topup_by_price(p_provider text, p_price_id text, p_product_id text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.resolve_credit_topup_by_price(p_provider text, p_price_id text, p_product_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.resolve_credit_topup_by_price(p_provider text, p_price_id text, p_product_id text) TO service_role;


--
-- Name: FUNCTION revoke_credits_by_tx_type(p_user_id uuid, p_tx_type text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.revoke_credits_by_tx_type(p_user_id uuid, p_tx_type text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.revoke_credits_by_tx_type(p_user_id uuid, p_tx_type text) TO service_role;


--
-- Name: FUNCTION set_active_bursar_config(p_config jsonb, p_label text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.set_active_bursar_config(p_config jsonb, p_label text) FROM PUBLIC;


--
-- Name: FUNCTION set_user_plan(p_user_id uuid, p_plan_key text, p_plan_assigned_at timestamp with time zone, p_config_version integer, p_allow_grandfathered boolean); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.set_user_plan(p_user_id uuid, p_plan_key text, p_plan_assigned_at timestamp with time zone, p_config_version integer, p_allow_grandfathered boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.set_user_plan(p_user_id uuid, p_plan_key text, p_plan_assigned_at timestamp with time zone, p_config_version integer, p_allow_grandfathered boolean) TO service_role;


--
-- Name: FUNCTION settle_lease(p_user_id uuid, p_lease_id uuid, p_amount numeric, p_idempotency_key text, p_min_balance numeric, p_model text, p_metadata jsonb, p_skip_allowance boolean, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.settle_lease(p_user_id uuid, p_lease_id uuid, p_amount numeric, p_idempotency_key text, p_min_balance numeric, p_model text, p_metadata jsonb, p_skip_allowance boolean, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.settle_lease(p_user_id uuid, p_lease_id uuid, p_amount numeric, p_idempotency_key text, p_min_balance numeric, p_model text, p_metadata jsonb, p_skip_allowance boolean, p_period_start date, p_feature text, p_feature_max_calls integer, p_feature_action text, p_feature_period_start date, p_feature_period_end date) TO service_role;


--
-- Name: FUNCTION snapshot_catalog_objects(p_version integer, p_config jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.snapshot_catalog_objects(p_version integer, p_config jsonb) FROM PUBLIC;


--
-- Name: FUNCTION spend_by_model(p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.spend_by_model(p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.spend_by_model(p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION spend_by_user(p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.spend_by_user(p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.spend_by_user(p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION sync_billing_from_config(p_config jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.sync_billing_from_config(p_config jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.sync_billing_from_config(p_config jsonb) TO service_role;


--
-- Name: FUNCTION sync_buckets_from_config(p_config jsonb, p_config_version integer); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.sync_buckets_from_config(p_config jsonb, p_config_version integer) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.sync_buckets_from_config(p_config jsonb, p_config_version integer) TO service_role;


--
-- Name: FUNCTION sync_plans_from_config(p_config jsonb, p_config_version integer); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.sync_plans_from_config(p_config jsonb, p_config_version integer) FROM PUBLIC;


--
-- Name: FUNCTION top_users(p_limit integer, p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.top_users(p_limit integer, p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.top_users(p_limit integer, p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION unset_user_plan(p_user_id uuid); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.unset_user_plan(p_user_id uuid) FROM PUBLIC;


--
-- Name: FUNCTION upsert_billing_customer(p_provider text, p_provider_customer_id text, p_user_id uuid, p_email text); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_customer(p_provider text, p_provider_customer_id text, p_user_id uuid, p_email text) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_customer(p_provider text, p_provider_customer_id text, p_user_id uuid, p_email text) TO service_role;


--
-- Name: FUNCTION upsert_billing_dispute(p_provider text, p_provider_dispute_id text, p_provider_payment_id text, p_user_id uuid, p_status text, p_reason text, p_metadata jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_dispute(p_provider text, p_provider_dispute_id text, p_provider_payment_id text, p_user_id uuid, p_status text, p_reason text, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_dispute(p_provider text, p_provider_dispute_id text, p_provider_payment_id text, p_user_id uuid, p_status text, p_reason text, p_metadata jsonb) TO service_role;


--
-- Name: FUNCTION upsert_billing_invoice(p_provider text, p_provider_invoice_id text, p_provider_subscription_id text, p_user_id uuid, p_status text, p_amount_paid_minor integer, p_amount_due_minor integer, p_currency text, p_period_start timestamp with time zone, p_period_end timestamp with time zone, p_metadata jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_invoice(p_provider text, p_provider_invoice_id text, p_provider_subscription_id text, p_user_id uuid, p_status text, p_amount_paid_minor integer, p_amount_due_minor integer, p_currency text, p_period_start timestamp with time zone, p_period_end timestamp with time zone, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_invoice(p_provider text, p_provider_invoice_id text, p_provider_subscription_id text, p_user_id uuid, p_status text, p_amount_paid_minor integer, p_amount_due_minor integer, p_currency text, p_period_start timestamp with time zone, p_period_end timestamp with time zone, p_metadata jsonb) TO service_role;


--
-- Name: FUNCTION upsert_billing_payment(p_provider text, p_provider_payment_id text, p_provider_invoice_id text, p_user_id uuid, p_amount_minor integer, p_tax_minor integer, p_currency text, p_purpose text, p_metadata jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_payment(p_provider text, p_provider_payment_id text, p_provider_invoice_id text, p_user_id uuid, p_amount_minor integer, p_tax_minor integer, p_currency text, p_purpose text, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_payment(p_provider text, p_provider_payment_id text, p_provider_invoice_id text, p_user_id uuid, p_amount_minor integer, p_tax_minor integer, p_currency text, p_purpose text, p_metadata jsonb) TO service_role;


--
-- Name: FUNCTION upsert_billing_preferences(p_user_id uuid, p_auto_recharge boolean, p_overage_protection boolean, p_email_notifications boolean, p_usage_alerts boolean, p_invoice_reminders boolean, p_usage_limit_alerts boolean); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_preferences(p_user_id uuid, p_auto_recharge boolean, p_overage_protection boolean, p_email_notifications boolean, p_usage_alerts boolean, p_invoice_reminders boolean, p_usage_limit_alerts boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_preferences(p_user_id uuid, p_auto_recharge boolean, p_overage_protection boolean, p_email_notifications boolean, p_usage_alerts boolean, p_invoice_reminders boolean, p_usage_limit_alerts boolean) TO service_role;


--
-- Name: FUNCTION upsert_billing_refund(p_provider text, p_provider_refund_id text, p_provider_payment_id text, p_user_id uuid, p_amount_minor integer, p_currency text, p_reason text, p_metadata jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_refund(p_provider text, p_provider_refund_id text, p_provider_payment_id text, p_user_id uuid, p_amount_minor integer, p_currency text, p_reason text, p_metadata jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_refund(p_provider text, p_provider_refund_id text, p_provider_payment_id text, p_user_id uuid, p_amount_minor integer, p_currency text, p_reason text, p_metadata jsonb) TO service_role;


--
-- Name: FUNCTION upsert_billing_subscription(p_state jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.upsert_billing_subscription(p_state jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION bursar.upsert_billing_subscription(p_state jsonb) TO service_role;


--
-- Name: FUNCTION validate_bursar_config(p_config jsonb); Type: ACL; Schema: bursar; Owner: -
--

REVOKE ALL ON FUNCTION bursar.validate_bursar_config(p_config jsonb) FROM PUBLIC;


--
