
-- ============================================================================
-- Source: 050_policies.sql
-- ============================================================================

-- Name: bursar_config Server-only Bursar config; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only Bursar config" ON bursar.bursar_config USING (false);


--
-- Name: billing_credit_topups Server-only billing_credit_topups; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_credit_topups" ON bursar.billing_credit_topups USING (false);


--
-- Name: billing_customers Server-only billing_customers; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_customers" ON bursar.billing_customers USING (false);


--
-- Name: billing_disputes Server-only billing_disputes; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_disputes" ON bursar.billing_disputes USING (false);


--
-- Name: billing_events Server-only billing_events; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_events" ON bursar.billing_events USING (false);


--
-- Name: billing_invoices Server-only billing_invoices; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_invoices" ON bursar.billing_invoices USING (false);


--
-- Name: billing_offers Server-only billing_offers; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_offers" ON bursar.billing_offers USING (false);


--
-- Name: billing_payments Server-only billing_payments; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_payments" ON bursar.billing_payments USING (false);


--
-- Name: billing_preferences Server-only billing_preferences; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_preferences" ON bursar.billing_preferences USING (false);


--
-- Name: billing_provider_refs Server-only billing_provider_refs; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_provider_refs" ON bursar.billing_provider_refs USING (false);


--
-- Name: billing_refunds Server-only billing_refunds; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_refunds" ON bursar.billing_refunds USING (false);


--
-- Name: billing_subscriptions Server-only billing_subscriptions; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only billing_subscriptions" ON bursar.billing_subscriptions USING (false);


--
-- Name: credit_buckets Server-only credit_buckets; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_buckets" ON bursar.credit_buckets USING (false);


--
-- Name: credit_plan_migrations Server-only credit_plan_migrations; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_plan_migrations" ON bursar.credit_plan_migrations USING (false);


--
-- Name: credit_plans Server-only credit_plans; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_plans" ON bursar.credit_plans USING (false);


--
-- Name: credit_spend_caps Server-only credit_spend_caps; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_spend_caps" ON bursar.credit_spend_caps USING (false);


--
-- Name: credit_team_members Server-only credit_team_members; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_team_members" ON bursar.credit_team_members USING (false);


--
-- Name: credit_teams Server-only credit_teams; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_teams" ON bursar.credit_teams USING (false);


--
-- Name: credit_usage_window Server-only credit_usage_window; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only credit_usage_window" ON bursar.credit_usage_window USING (false);


--
-- Name: signup_grant_failures Server-only signup_grant_failures; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only signup_grant_failures" ON bursar.signup_grant_failures USING (false);


--
-- Name: user_credit_buckets Server-only user_credit_buckets; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Server-only user_credit_buckets" ON bursar.user_credit_buckets USING (false);


--
CREATE POLICY "Server-only credit_accounts" ON bursar.credit_accounts USING (false);
CREATE POLICY "Server-only credit_ledger_entries" ON bursar.credit_ledger_entries USING (false);
CREATE POLICY "Server-only credit_lot_allocations" ON bursar.credit_lot_allocations USING (false);
CREATE POLICY "Server-only credit_lot_reversals" ON bursar.credit_lot_reversals USING (false);
CREATE POLICY "Server-only credit_lots" ON bursar.credit_lots USING (false);
CREATE POLICY "Server-only catalog_object_versions" ON bursar.catalog_object_versions USING (false);
CREATE POLICY "Server-only credit_reservations" ON bursar.credit_reservations USING (false);


--


-- ============================================================================
-- Source: 060_rls.sql
-- ============================================================================

-- Name: billing_credit_topups; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_credit_topups ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_customers; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_customers ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_disputes; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_disputes ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_events; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_events ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_invoices; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_invoices ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_offers; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_offers ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_payments; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_payments ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_preferences; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_preferences ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_provider_refs; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_provider_refs ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_refunds; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_refunds ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_subscriptions; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.billing_subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: bursar_config; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.bursar_config ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_buckets; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_buckets ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_plan_migrations; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_plan_migrations ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_plans; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_plans ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_reservations; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_reservations ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_spend_caps; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_spend_caps ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_team_members; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_team_members ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_teams; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_teams ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_transactions; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_transactions ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_usage_window; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.credit_usage_window ENABLE ROW LEVEL SECURITY;

--
-- Name: signup_grant_failures; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.signup_grant_failures ENABLE ROW LEVEL SECURITY;

--
-- Name: user_credit_buckets; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.user_credit_buckets ENABLE ROW LEVEL SECURITY;

--
-- Name: user_credits; Type: ROW SECURITY; Schema: bursar; Owner: -
--

ALTER TABLE bursar.user_credits ENABLE ROW LEVEL SECURITY;

ALTER TABLE bursar.credit_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.credit_ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.credit_lot_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.credit_lot_reversals ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.credit_lots ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.catalog_object_versions ENABLE ROW LEVEL SECURITY;

--

