-- Name: billing_subscriptions billing_subscriptions_offer_key_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_offer_key_fkey FOREIGN KEY (offer_key) REFERENCES bursar.billing_offers(offer_key);


--
-- Name: billing_subscriptions billing_subscriptions_plan_version_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_plan_version_id_fkey FOREIGN KEY (plan_version_id) REFERENCES bursar.credit_plans(id);


--
-- Name: catalog_object_versions catalog_object_versions_config_version_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.catalog_object_versions
    ADD CONSTRAINT catalog_object_versions_config_version_fkey FOREIGN KEY (config_version) REFERENCES bursar.bursar_config(version) ON DELETE RESTRICT;


--
-- Name: credit_ledger_entries credit_ledger_entries_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_ledger_entries
    ADD CONSTRAINT credit_ledger_entries_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);


--
-- Name: credit_lot_allocations credit_lot_allocations_debit_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_debit_entry_id_fkey FOREIGN KEY (debit_entry_id) REFERENCES bursar.credit_ledger_entries(id);


--
-- Name: credit_lot_allocations credit_lot_allocations_lot_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_allocations
    ADD CONSTRAINT credit_lot_allocations_lot_id_fkey FOREIGN KEY (lot_id) REFERENCES bursar.credit_lots(id);


--
-- Name: credit_lot_reversals credit_lot_reversals_original_allocation_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_original_allocation_id_fkey FOREIGN KEY (original_allocation_id) REFERENCES bursar.credit_lot_allocations(id);


--
-- Name: credit_lot_reversals credit_lot_reversals_refund_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lot_reversals
    ADD CONSTRAINT credit_lot_reversals_refund_entry_id_fkey FOREIGN KEY (refund_entry_id) REFERENCES bursar.credit_ledger_entries(id);


--
-- Name: credit_lots credit_lots_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);


--
-- Name: credit_lots credit_lots_source_entry_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_lots
    ADD CONSTRAINT credit_lots_source_entry_id_fkey FOREIGN KEY (source_entry_id) REFERENCES bursar.credit_ledger_entries(id);


--
-- Name: credit_plan_migrations credit_plan_migrations_from_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_from_plan_id_fkey FOREIGN KEY (from_plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: credit_plan_migrations credit_plan_migrations_to_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_to_plan_id_fkey FOREIGN KEY (to_plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: credit_plan_migrations credit_plan_migrations_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_plan_migrations
    ADD CONSTRAINT credit_plan_migrations_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_reservations credit_reservations_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_reservations
    ADD CONSTRAINT credit_reservations_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_spend_caps credit_spend_caps_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_spend_caps
    ADD CONSTRAINT credit_spend_caps_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_team_members credit_team_members_team_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_team_id_fkey FOREIGN KEY (team_id) REFERENCES bursar.credit_teams(id) ON DELETE CASCADE;


--
-- Name: credit_team_members credit_team_members_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_team_members
    ADD CONSTRAINT credit_team_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_transactions credit_transactions_account_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_transactions
    ADD CONSTRAINT credit_transactions_account_id_fkey FOREIGN KEY (account_id) REFERENCES bursar.credit_accounts(id);


--
-- Name: credit_transactions credit_transactions_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_transactions
    ADD CONSTRAINT credit_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: credit_usage_window credit_usage_window_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_usage_window
    ADD CONSTRAINT credit_usage_window_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: credit_usage_window credit_usage_window_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.credit_usage_window
    ADD CONSTRAINT credit_usage_window_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: user_credit_buckets user_credit_buckets_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credit_buckets
    ADD CONSTRAINT user_credit_buckets_user_id_fkey FOREIGN KEY (user_id) REFERENCES bursar.user_credits(user_id) ON DELETE CASCADE;


--
-- Name: user_credits user_credits_plan_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credits
    ADD CONSTRAINT user_credits_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES bursar.credit_plans(id);


--
-- Name: user_credits user_credits_user_id_fkey; Type: FK CONSTRAINT; Schema: bursar; Owner: -
--

ALTER TABLE ONLY bursar.user_credits
    ADD CONSTRAINT user_credits_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;


--
