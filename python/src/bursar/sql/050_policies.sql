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
-- Name: user_credits Users can view own credits; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Users can view own credits" ON bursar.user_credits FOR SELECT USING ((auth.uid() = user_id));


--
-- Name: credit_reservations Users can view own reservations; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Users can view own reservations" ON bursar.credit_reservations FOR SELECT USING ((auth.uid() = user_id));


--
-- Name: credit_transactions Users can view own transactions; Type: POLICY; Schema: bursar; Owner: -
--

CREATE POLICY "Users can view own transactions" ON bursar.credit_transactions FOR SELECT USING ((auth.uid() = user_id));


--
