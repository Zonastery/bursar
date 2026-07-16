-- Name: bursar_config bursar_config_catalog_snapshot; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER bursar_config_catalog_snapshot AFTER INSERT OR UPDATE OF active ON bursar.bursar_config FOR EACH ROW EXECUTE FUNCTION bursar.capture_activated_catalog_snapshot();


--
-- Name: bursar_config bursar_config_immutable; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER bursar_config_immutable BEFORE UPDATE ON bursar.bursar_config FOR EACH ROW EXECUTE FUNCTION bursar.prevent_bursar_config_payload_mutation();


--
-- Name: credit_ledger_entries credit_ledger_lot_allocation; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER credit_ledger_lot_allocation AFTER INSERT ON bursar.credit_ledger_entries FOR EACH ROW EXECUTE FUNCTION bursar.allocate_ledger_entry_lots();


--
-- Name: credit_ledger_entries credit_ledger_refund_lot_provenance; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER credit_ledger_refund_lot_provenance AFTER INSERT ON bursar.credit_ledger_entries FOR EACH ROW EXECUTE FUNCTION bursar.record_refund_lot_provenance();


--
-- Name: credit_transactions credit_transaction_account_assignment; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER credit_transaction_account_assignment BEFORE INSERT ON bursar.credit_transactions FOR EACH ROW EXECUTE FUNCTION bursar.assign_credit_account();


--
-- Name: credit_transactions credit_transaction_ledger_projection; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE CONSTRAINT TRIGGER credit_transaction_ledger_projection AFTER INSERT ON bursar.credit_transactions DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION bursar.project_credit_transaction();


--
-- Name: billing_credit_topups set_billing_credit_topups_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_credit_topups_updated_at BEFORE UPDATE ON bursar.billing_credit_topups FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_customers set_billing_customers_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_customers_updated_at BEFORE UPDATE ON bursar.billing_customers FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_disputes set_billing_disputes_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_disputes_updated_at BEFORE UPDATE ON bursar.billing_disputes FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_events set_billing_events_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_events_updated_at BEFORE UPDATE ON bursar.billing_events FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_invoices set_billing_invoices_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_invoices_updated_at BEFORE UPDATE ON bursar.billing_invoices FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_offers set_billing_offers_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_offers_updated_at BEFORE UPDATE ON bursar.billing_offers FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_payments set_billing_payments_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_payments_updated_at BEFORE UPDATE ON bursar.billing_payments FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_preferences set_billing_preferences_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_preferences_updated_at BEFORE UPDATE ON bursar.billing_preferences FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_provider_refs set_billing_provider_refs_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_provider_refs_updated_at BEFORE UPDATE ON bursar.billing_provider_refs FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_refunds set_billing_refunds_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_refunds_updated_at BEFORE UPDATE ON bursar.billing_refunds FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: billing_subscriptions set_billing_subscriptions_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_billing_subscriptions_updated_at BEFORE UPDATE ON bursar.billing_subscriptions FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: credit_buckets set_credit_buckets_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_credit_buckets_updated_at BEFORE UPDATE ON bursar.credit_buckets FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: user_credit_buckets set_user_credit_buckets_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_user_credit_buckets_updated_at BEFORE UPDATE ON bursar.user_credit_buckets FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
-- Name: user_credits set_user_credits_updated_at; Type: TRIGGER; Schema: bursar; Owner: -
--

CREATE TRIGGER set_user_credits_updated_at BEFORE UPDATE ON bursar.user_credits FOR EACH ROW EXECUTE FUNCTION bursar.handle_updated_at();


--
