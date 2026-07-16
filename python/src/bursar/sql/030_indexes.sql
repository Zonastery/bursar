-- Name: billing_events_claimable_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX billing_events_claimable_idx ON bursar.billing_events USING btree (created_at, id) WHERE (status = ANY (ARRAY['processing'::text, 'failed'::text]));


--
-- Name: billing_provider_refs_lookup_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_lookup_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, lookup_key) WHERE (lookup_key IS NOT NULL);


--
-- Name: billing_provider_refs_price_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_price_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, price_id) WHERE (price_id IS NOT NULL);


--
-- Name: billing_provider_refs_product_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_product_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, product_id) WHERE (product_id IS NOT NULL);


--
-- Name: billing_provider_refs_variant_environment_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX billing_provider_refs_variant_environment_uq ON bursar.billing_provider_refs USING btree (provider, environment, variant_id) WHERE (variant_id IS NOT NULL);


--
-- Name: catalog_object_versions_type_key_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX catalog_object_versions_type_key_idx ON bursar.catalog_object_versions USING btree (object_type, object_key, config_version DESC);


--
-- Name: credit_accounts_personal_owner_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX credit_accounts_personal_owner_uq ON bursar.credit_accounts USING btree (user_id) WHERE (account_type = 'personal'::text);


--
-- Name: credit_accounts_team_owner_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX credit_accounts_team_owner_uq ON bursar.credit_accounts USING btree (team_id) WHERE (account_type = 'team'::text);


--
-- Name: credit_ledger_account_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_ledger_account_cursor_idx ON bursar.credit_ledger_entries USING btree (account_id, created_at DESC, id DESC);


--
-- Name: credit_lot_allocations_lot_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_lot_allocations_lot_idx ON bursar.credit_lot_allocations USING btree (lot_id, created_at DESC) WHERE (lot_id IS NOT NULL);


--
-- Name: credit_lot_reversals_original_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_lot_reversals_original_idx ON bursar.credit_lot_reversals USING btree (original_allocation_id, created_at DESC);


--
-- Name: credit_lots_active_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_lots_active_idx ON bursar.credit_lots USING btree (account_id, expires_at) WHERE (consumed < granted);


--
-- Name: credit_transactions_account_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_transactions_account_cursor_idx ON bursar.credit_transactions USING btree (account_id, created_at DESC, id DESC);


--
-- Name: credit_transactions_operation_key_uq; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX credit_transactions_operation_key_uq ON bursar.credit_transactions USING btree (user_id, type, idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: credit_transactions_user_cursor_idx; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX credit_transactions_user_cursor_idx ON bursar.credit_transactions USING btree (user_id, created_at DESC, id DESC);


--
-- Name: idx_billing_customers_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_customers_provider ON bursar.billing_customers USING btree (provider, provider_customer_id);


--
-- Name: idx_billing_customers_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_customers_user ON bursar.billing_customers USING btree (user_id);


--
-- Name: idx_billing_disputes_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_disputes_provider ON bursar.billing_disputes USING btree (provider, provider_dispute_id);


--
-- Name: idx_billing_disputes_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_disputes_user ON bursar.billing_disputes USING btree (user_id);


--
-- Name: idx_billing_events_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_events_provider ON bursar.billing_events USING btree (provider, provider_event_id);


--
-- Name: idx_billing_events_status; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_events_status ON bursar.billing_events USING btree (status);


--
-- Name: idx_billing_invoices_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_invoices_provider ON bursar.billing_invoices USING btree (provider, provider_invoice_id);


--
-- Name: idx_billing_invoices_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_invoices_user ON bursar.billing_invoices USING btree (user_id);


--
-- Name: idx_billing_offers_plan; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_offers_plan ON bursar.billing_offers USING btree (plan);


--
-- Name: idx_billing_payments_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_payments_provider ON bursar.billing_payments USING btree (provider, provider_payment_id);


--
-- Name: idx_billing_payments_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_payments_user ON bursar.billing_payments USING btree (user_id);


--
-- Name: idx_billing_provider_refs_resource; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_provider_refs_resource ON bursar.billing_provider_refs USING btree (resource_type, resource_key);


--
-- Name: idx_billing_refunds_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_refunds_provider ON bursar.billing_refunds USING btree (provider, provider_refund_id);


--
-- Name: idx_billing_subscriptions_provider; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_billing_subscriptions_provider ON bursar.billing_subscriptions USING btree (provider, provider_subscription_id);


--
-- Name: idx_billing_subscriptions_status; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_subscriptions_status ON bursar.billing_subscriptions USING btree (status);


--
-- Name: idx_billing_subscriptions_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_billing_subscriptions_user ON bursar.billing_subscriptions USING btree (user_id);


--
-- Name: idx_bursar_config_active_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_bursar_config_active_unique ON bursar.bursar_config USING btree (active) WHERE (active = true);


--
-- Name: idx_bursar_config_version_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_bursar_config_version_unique ON bursar.bursar_config USING btree (version);


--
-- Name: idx_credit_buckets_single_default; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_buckets_single_default ON bursar.credit_buckets USING btree (is_default) WHERE (is_default = true);


--
-- Name: idx_credit_buckets_single_overdraft; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_buckets_single_overdraft ON bursar.credit_buckets USING btree (allow_overdraft) WHERE (allow_overdraft = true);


--
-- Name: idx_credit_plan_migrations_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_plan_migrations_user ON bursar.credit_plan_migrations USING btree (user_id, created_at DESC);


--
-- Name: idx_credit_plans_plan_key; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_plans_plan_key ON bursar.credit_plans USING btree (plan_key);


--
-- Name: idx_credit_plans_plan_key_version; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_plans_plan_key_version ON bursar.credit_plans USING btree (plan_key, config_version) WHERE (plan_key IS NOT NULL);


--
-- Name: idx_credit_reservations_active; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_reservations_active ON bursar.credit_reservations USING btree (user_id, operation_type, status, expires_at);


--
-- Name: idx_credit_reservations_user_expires; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_reservations_user_expires ON bursar.credit_reservations USING btree (user_id, expires_at);


--
-- Name: idx_credit_spend_caps_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_spend_caps_unique ON bursar.credit_spend_caps USING btree (user_id, cap_type, COALESCE(model, ''::text));


--
-- Name: idx_credit_transactions_created_at; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_created_at ON bursar.credit_transactions USING btree (created_at);


--
-- Name: idx_credit_transactions_expires_at; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_expires_at ON bursar.credit_transactions USING btree (((metadata ->> 'expires_at'::text))) WHERE ((metadata ? 'expires_at'::text) AND (NOT (metadata ? 'swept_at'::text)));


--
-- Name: idx_credit_transactions_idempotency_team_usage; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_transactions_idempotency_team_usage ON bursar.credit_transactions USING btree (user_id, type, ((metadata ->> 'team_id'::text)), ((metadata ->> 'idempotency_key'::text))) WHERE ((type = 'team_usage'::bursar.credit_tx_type) AND ((metadata ->> 'idempotency_key'::text) IS NOT NULL));


--
-- Name: idx_credit_transactions_idempotency_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_transactions_idempotency_user ON bursar.credit_transactions USING btree (user_id, type, ((metadata ->> 'idempotency_key'::text))) WHERE (((metadata ->> 'idempotency_key'::text) IS NOT NULL) AND (type <> 'team_usage'::bursar.credit_tx_type));


--
-- Name: idx_credit_transactions_reference_id; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_reference_id ON bursar.credit_transactions USING btree (reference_id) WHERE (reference_id IS NOT NULL);


--
-- Name: idx_credit_transactions_type_created; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_type_created ON bursar.credit_transactions USING btree (type, created_at DESC);


--
-- Name: idx_credit_transactions_user_expires; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_user_expires ON bursar.credit_transactions USING btree (user_id, ((metadata ->> 'expires_at'::text))) WHERE ((metadata ? 'expires_at'::text) AND (NOT (metadata ? 'swept_at'::text)));


--
-- Name: idx_credit_transactions_user_id; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_user_id ON bursar.credit_transactions USING btree (user_id, created_at DESC);


--
-- Name: idx_credit_transactions_user_id_created_at; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_transactions_user_id_created_at ON bursar.credit_transactions USING btree (user_id, created_at);


--
-- Name: idx_credit_usage_window_plan_id; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_credit_usage_window_plan_id ON bursar.credit_usage_window USING btree (plan_id);


--
-- Name: idx_credit_usage_window_unique; Type: INDEX; Schema: bursar; Owner: -
--

CREATE UNIQUE INDEX idx_credit_usage_window_unique ON bursar.credit_usage_window USING btree (user_id, plan_id, billing_period);


--
-- Name: idx_signup_grant_failures_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_signup_grant_failures_user ON bursar.signup_grant_failures USING btree (user_id, created_at DESC);


--
-- Name: idx_user_credit_buckets_user; Type: INDEX; Schema: bursar; Owner: -
--

CREATE INDEX idx_user_credit_buckets_user ON bursar.user_credit_buckets USING btree (user_id);


--
