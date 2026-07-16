-- Name: billing_credit_topups billing_credit_topups_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_credit_topups
    ADD CONSTRAINT billing_credit_topups_pkey PRIMARY KEY (topup_key);


--
-- Name: billing_customers billing_customers_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_customers
    ADD CONSTRAINT billing_customers_pkey PRIMARY KEY (id);


--
-- Name: billing_disputes billing_disputes_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_disputes
    ADD CONSTRAINT billing_disputes_pkey PRIMARY KEY (id);


--
-- Name: billing_events billing_events_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_events
    ADD CONSTRAINT billing_events_pkey PRIMARY KEY (id);


--
-- Name: billing_invoices billing_invoices_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_invoices
    ADD CONSTRAINT billing_invoices_pkey PRIMARY KEY (id);


--
-- Name: billing_offers billing_offers_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_offers
    ADD CONSTRAINT billing_offers_pkey PRIMARY KEY (offer_key);


--
-- Name: billing_payments billing_payments_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_payments
    ADD CONSTRAINT billing_payments_pkey PRIMARY KEY (id);


--
-- Name: billing_preferences billing_preferences_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_preferences
    ADD CONSTRAINT billing_preferences_pkey PRIMARY KEY (user_id);


--
-- Name: billing_provider_refs billing_provider_refs_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_provider_refs
    ADD CONSTRAINT billing_provider_refs_pkey PRIMARY KEY (id);


--
-- Name: billing_refunds billing_refunds_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_refunds
    ADD CONSTRAINT billing_refunds_pkey PRIMARY KEY (id);


--
-- Name: billing_subscriptions billing_subscriptions_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: bursar_config bursar_config_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.bursar_config
    ADD CONSTRAINT bursar_config_pkey PRIMARY KEY (id);


--
-- Name: catalog_object_versions catalog_object_versions_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.catalog_object_versions
    ADD CONSTRAINT catalog_object_versions_pkey PRIMARY KEY (config_version, object_type, object_key);


--
-- Name: credit_accounts credit_accounts_account_type_user_id_team_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_accounts
    ADD CONSTRAINT credit_accounts_account_type_user_id_team_id_key UNIQUE (account_type, user_id, team_id);


--
-- Name: credit_accounts credit_accounts_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_accounts
    ADD CONSTRAINT credit_accounts_pkey PRIMARY KEY (id);


--
-- Name: credit_buckets credit_buckets_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_buckets
    ADD CONSTRAINT credit_buckets_pkey PRIMARY KEY (bucket_key);


--
-- Name: credit_ledger_entries credit_ledger_entries_account_id_entry_type_idempotency_key_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_account_id_entry_type_idempotency_key_key UNIQUE (account_id, entry_type, idempotency_key);


--
-- Name: credit_ledger_entries credit_ledger_entries_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_pkey PRIMARY KEY (id);


--
-- Name: credit_lot_allocations credit_lot_allocations_debit_entry_id_lot_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_debit_entry_id_lot_id_key UNIQUE (debit_entry_id, lot_id);


--
-- Name: credit_lot_allocations credit_lot_allocations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_pkey PRIMARY KEY (id);


--
-- Name: credit_lot_reversals credit_lot_reversals_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_pkey PRIMARY KEY (id);


--
-- Name: credit_lot_reversals credit_lot_reversals_refund_entry_id_original_allocation_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_refund_entry_id_original_allocation_id_key UNIQUE (refund_entry_id, original_allocation_id);


--
-- Name: credit_lots credit_lots_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_pkey PRIMARY KEY (id);


--
-- Name: credit_lots credit_lots_source_entry_unique; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_source_entry_unique UNIQUE (source_entry_id);


--
-- Name: credit_plan_migrations credit_plan_migrations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_pkey PRIMARY KEY (id);


--
-- Name: credit_plans credit_plans_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plans
    ADD CONSTRAINT credit_plans_pkey PRIMARY KEY (id);


--
-- Name: credit_reservations credit_reservations_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_reservations
    ADD CONSTRAINT credit_reservations_pkey PRIMARY KEY (id);


--
-- Name: credit_spend_caps credit_spend_caps_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_spend_caps
    ADD CONSTRAINT credit_spend_caps_pkey PRIMARY KEY (id);


--
-- Name: credit_team_members credit_team_members_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_pkey PRIMARY KEY (id);


--
-- Name: credit_team_members credit_team_members_team_id_user_id_key; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_team_id_user_id_key UNIQUE (team_id, user_id);


--
-- Name: credit_teams credit_teams_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_teams
    ADD CONSTRAINT credit_teams_pkey PRIMARY KEY (id);


--
-- Name: credit_transactions credit_transactions_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_transactions
    ADD CONSTRAINT credit_transactions_pkey PRIMARY KEY (id);


--
-- Name: credit_usage_window credit_usage_window_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_usage_window
    ADD CONSTRAINT credit_usage_window_pkey PRIMARY KEY (id);


--
-- Name: signup_grant_failures signup_grant_failures_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.signup_grant_failures
    ADD CONSTRAINT signup_grant_failures_pkey PRIMARY KEY (id);


--
-- Name: user_credit_buckets user_credit_buckets_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credit_buckets
    ADD CONSTRAINT user_credit_buckets_pkey PRIMARY KEY (user_id, bucket_key);


--
-- Name: user_credits user_credits_pkey; Type: CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credits
    ADD CONSTRAINT user_credits_pkey PRIMARY KEY (user_id);


--
