// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"
	"time"
)

func TestPostgresBillingStateProjectionHelpers(t *testing.T) {
	t.Run("required text", func(t *testing.T) {
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			want    string
			wantErr bool
		}{
			{"missing row", nil, "", true},
			{"multiple rows use first", []map[string]any{{"id": "one"}, {"id": "two"}}, "one", false},
			{"missing scalar", []map[string]any{{}}, "", true},
			{"invalid scalar", []map[string]any{{"id": nil}}, "", true},
			{"success", []map[string]any{{"id": "value"}}, "value", false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := billingRequiredTextResult(test.rows, "text_projection")
				if (err != nil) != test.wantErr || got != test.want {
					t.Fatalf("projection = %q, %v; want %q, error=%v", got, err, test.want, test.wantErr)
				}
			})
		}
	})

	t.Run("credit posting", func(t *testing.T) {
		base := map[string]any{
			"ledger_entry_id": "ledger", "balance_after": "12.345678", "replayed": true, "error_code": "",
		}
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			wantErr bool
		}{
			{"missing row", nil, true},
			{"invalid balance", []map[string]any{{"balance_after": "bad", "replayed": true}}, true},
			{"invalid replay", []map[string]any{{"balance_after": "1", "replayed": "bad"}}, true},
			{"success", []map[string]any{base}, false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := billingCreditPostingResultFromRows(test.rows, "posting_projection")
				if (err != nil) != test.wantErr {
					t.Fatalf("projection = %+v, error = %v; want error=%v", got, err, test.wantErr)
				}
				if !test.wantErr && (got.LedgerEntryID != "ledger" || !got.Replayed || got.BalanceAfter == nil || !got.BalanceAfter.Equal(MustAmount("12.345678"))) {
					t.Fatalf("posting projection = %+v", got)
				}
			})
		}
	})

	t.Run("top-up", func(t *testing.T) {
		base := map[string]any{
			"id": "topup", "topup_key": "credits", "credits_per_unit": "10.25", "bucket_key": "general",
			"amount_minor": int64(500), "currency": "USD", "min_quantity": 1, "max_quantity": 10, "default_quantity": 2,
		}
		invalid := func(mutate func(map[string]any)) []map[string]any {
			row := cloneAnyMap(base)
			mutate(row)
			return []map[string]any{row}
		}
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			wantNil bool
			wantErr bool
		}{
			{"missing", nil, true, false},
			{"multiple", []map[string]any{base, base}, false, true},
			{"id", invalid(func(row map[string]any) { delete(row, "id") }), false, true},
			{"key", invalid(func(row map[string]any) { delete(row, "topup_key") }), false, true},
			{"credits malformed", invalid(func(row map[string]any) { row["credits_per_unit"] = "bad" }), false, true},
			{"credits non-positive", invalid(func(row map[string]any) { row["credits_per_unit"] = "0" }), false, true},
			{"amount malformed", invalid(func(row map[string]any) { row["amount_minor"] = "bad" }), false, true},
			{"amount negative", invalid(func(row map[string]any) { row["amount_minor"] = int64(-1) }), false, true},
			{"currency", invalid(func(row map[string]any) { row["currency"] = "US" }), false, true},
			{"minimum malformed", invalid(func(row map[string]any) { row["min_quantity"] = "bad" }), false, true},
			{"maximum malformed", invalid(func(row map[string]any) { row["max_quantity"] = "bad" }), false, true},
			{"default malformed", invalid(func(row map[string]any) { row["default_quantity"] = "bad" }), false, true},
			{"minimum bound", invalid(func(row map[string]any) { row["min_quantity"] = 0 }), false, true},
			{"maximum bound", invalid(func(row map[string]any) { row["max_quantity"] = 0 }), false, true},
			{"default lower bound", invalid(func(row map[string]any) { row["default_quantity"] = 0 }), false, true},
			{"default upper bound", invalid(func(row map[string]any) { row["default_quantity"] = 11 }), false, true},
			{"safe-minor overflow", invalid(func(row map[string]any) { row["amount_minor"], row["max_quantity"] = int64(9_007_199_254_740_991), 2 }), false, true},
			{"success", []map[string]any{base}, false, false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := billingTopupResultFromRows(test.rows, "topup_projection")
				if (err != nil) != test.wantErr || (!test.wantErr && (got == nil) != test.wantNil) {
					t.Fatalf("projection = %+v, error = %v; want nil=%v, error=%v", got, err, test.wantNil, test.wantErr)
				}
				if !test.wantErr && !test.wantNil && (got.MinAmountMinor != 500 || got.MaxAmountMinor != 5_000 || !got.CreditsPerUnit.Equal(MustAmount("10.25"))) {
					t.Fatalf("top-up projection = %+v", got)
				}
			})
		}
	})

	t.Run("single optional row", func(t *testing.T) {
		if row, err := billingOptionalSingleRow(nil, "lookup", "records"); err != nil || row != nil {
			t.Fatalf("missing row = %#v, %v", row, err)
		}
		if _, err := billingOptionalSingleRow([]map[string]any{{"id": "one"}, {"id": "two"}}, "lookup", "records"); err == nil {
			t.Fatal("multiple rows were accepted")
		}
		row, err := billingOptionalSingleRow([]map[string]any{{"id": "one"}}, "lookup", "records")
		if err != nil || optionalRowText(row, "id") != "one" {
			t.Fatalf("single row = %#v, %v", row, err)
		}
	})

	t.Run("provider identity", func(t *testing.T) {
		customerRow := map[string]any{"provider": "alpha", "provider_customer_id": "cus", "subject_id": "account"}
		if _, err := billingCustomerByProviderFromRow(map[string]any{}, "alpha", "cus"); err == nil {
			t.Fatal("malformed customer was accepted")
		}
		if _, err := billingCustomerByProviderFromRow(customerRow, "alpha", "other"); err == nil {
			t.Fatal("mismatched customer was accepted")
		}
		if customer, err := billingCustomerByProviderFromRow(customerRow, "alpha", "cus"); err != nil || customer.AccountID != "account" {
			t.Fatalf("customer = %+v, %v", customer, err)
		}

		now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
		paymentRow := map[string]any{
			"id": "payment", "provider": "alpha", "provider_payment_id": "pay", "subject_id": "account",
			"amount_minor": int64(1), "tax_minor": int64(0), "currency": "USD", "purpose": "subscription",
			"status": "succeeded", "provider_updated_at": now, "metadata": `{}`,
		}
		if _, err := billingPaymentByProviderFromRow(map[string]any{}, "alpha", "pay"); err == nil {
			t.Fatal("malformed payment was accepted")
		}
		if _, err := billingPaymentByProviderFromRow(paymentRow, "alpha", "other"); err == nil {
			t.Fatal("mismatched payment was accepted")
		}
		if payment, err := billingPaymentByProviderFromRow(paymentRow, "alpha", "pay"); err != nil || payment.ID != "payment" {
			t.Fatalf("payment = %+v, %v", payment, err)
		}
	})

	t.Run("boolean and optional ID results", func(t *testing.T) {
		for _, rows := range [][]map[string]any{nil, []map[string]any{{}}, []map[string]any{{"value": "bad"}}} {
			if _, err := billingRequiredBoolResult(rows, "bool_projection"); err == nil {
				t.Fatalf("invalid required bool rows accepted: %#v", rows)
			}
		}
		if value, err := billingRequiredBoolResult([]map[string]any{{"value": true}}, "bool_projection"); err != nil || !value {
			t.Fatalf("required bool = %v, %v", value, err)
		}
		if value, err := billingOptionalBoolResult(nil, "bool_projection"); err != nil || value {
			t.Fatalf("missing optional bool = %v, %v", value, err)
		}
		if value, err := billingOptionalBoolResult([]map[string]any{{}}, "bool_projection"); err != nil || value {
			t.Fatalf("empty optional bool = %v, %v", value, err)
		}
		if _, err := billingOptionalBoolResult([]map[string]any{{"one": true, "two": false}}, "bool_projection"); err == nil {
			t.Fatal("multi-scalar optional bool was accepted")
		}
		if _, err := billingOptionalBoolResult([]map[string]any{{"value": "bad"}}, "bool_projection"); err == nil {
			t.Fatal("invalid optional bool was accepted")
		}
		if value, err := billingOptionalBoolResult([]map[string]any{{"value": false}}, "bool_projection"); err != nil || value {
			t.Fatalf("optional bool = %v, %v", value, err)
		}
		if id, err := billingOptionalIDResult(nil, "id_projection"); err != nil || id != "" {
			t.Fatalf("missing optional ID = %q, %v", id, err)
		}
		if _, err := billingOptionalIDResult([]map[string]any{{"id": " "}}, "id_projection"); err == nil {
			t.Fatal("invalid optional ID was accepted")
		}
		if id, err := billingOptionalIDResult([]map[string]any{{"id": "grant"}}, "id_projection"); err != nil || id != "grant" {
			t.Fatalf("optional ID = %q, %v", id, err)
		}
	})
}

func TestPostgresCommerceStateProjectionHelpers(t *testing.T) {
	t.Run("required text", func(t *testing.T) {
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			want    string
			wantErr bool
		}{
			{"missing row", nil, "", true},
			{"missing scalar", []map[string]any{{}}, "", true},
			{"invalid scalar", []map[string]any{{"id": nil}}, "", true},
			{"success", []map[string]any{{"id": "value"}}, "value", false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := commerceStateRequiredTextResult(test.rows, "text_projection")
				if (err != nil) != test.wantErr || got != test.want {
					t.Fatalf("projection = %q, %v; want %q, error=%v", got, err, test.want, test.wantErr)
				}
			})
		}
	})

	t.Run("subscription selection", func(t *testing.T) {
		now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
		allowed := map[string]bool{"active": true}
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			wantID  string
			wantErr bool
		}{
			{"empty", nil, "", false},
			{"missing status", []map[string]any{{"provider_updated_at": now}}, "", true},
			{"invalid timestamp", []map[string]any{{"status": "active", "provider_updated_at": "bad"}}, "", true},
			{"filtered", []map[string]any{{"id": "past", "status": "past_due", "provider_updated_at": now}}, "", false},
			{"newest allowed", []map[string]any{
				{"id": "old", "status": "active", "provider_updated_at": now},
				{"id": "filtered", "status": "past_due", "provider_updated_at": now.Add(2 * time.Hour)},
				{"id": "new", "status": "active", "provider_updated_at": now.Add(time.Hour)},
			}, "new", false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := commerceStateSelectSubscriptionRow(test.rows, allowed, "subscription_projection")
				if (err != nil) != test.wantErr {
					t.Fatalf("selection = %#v, error = %v; want error=%v", got, err, test.wantErr)
				}
				if !test.wantErr && optionalRowText(got, "id") != test.wantID {
					t.Fatalf("selected ID = %q, want %q", optionalRowText(got, "id"), test.wantID)
				}
			})
		}
	})

	t.Run("required bool", func(t *testing.T) {
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			want    bool
			wantErr bool
		}{
			{"missing row", nil, false, true},
			{"missing scalar", []map[string]any{{}}, false, true},
			{"invalid scalar", []map[string]any{{"value": "bad"}}, false, true},
			{"false", []map[string]any{{"value": false}}, false, false},
			{"true", []map[string]any{{"value": true}}, true, false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := commerceStateRequiredBoolResult(test.rows, "bool_projection")
				if (err != nil) != test.wantErr || got != test.want {
					t.Fatalf("projection = %v, %v; want %v, error=%v", got, err, test.want, test.wantErr)
				}
			})
		}
	})

	t.Run("attempt count", func(t *testing.T) {
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			want    int
			wantErr bool
		}{
			{"missing row", nil, 0, true},
			{"missing scalar", []map[string]any{{}}, 0, true},
			{"invalid scalar", []map[string]any{{"count": "bad"}}, 0, true},
			{"negative", []map[string]any{{"count": -1}}, 0, true},
			{"success", []map[string]any{{"count": 3}}, 3, false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := commerceStateCountResult(test.rows, "count_projection")
				if (err != nil) != test.wantErr || got != test.want {
					t.Fatalf("projection = %d, %v; want %d, error=%v", got, err, test.want, test.wantErr)
				}
			})
		}
	})

	t.Run("auto-recharge top-up", func(t *testing.T) {
		for _, test := range []struct {
			name    string
			rows    []map[string]any
			wantNil bool
			wantErr bool
		}{
			{"missing", nil, true, false},
			{"multiple", []map[string]any{{"id": "one"}, {"id": "two"}}, true, true},
			{"missing key", []map[string]any{{"id": "topup"}}, true, true},
			{"mismatch", []map[string]any{{"id": "topup", "topup_key": "other"}}, true, false},
			{"missing ID", []map[string]any{{"topup_key": "credits"}}, true, true},
			{"success", []map[string]any{{"id": "topup", "topup_key": "credits"}}, false, false},
		} {
			t.Run(test.name, func(t *testing.T) {
				got, err := commerceStateAutoRechargeTopupFromRows(test.rows, "alpha", "price_id", "credits", "price")
				if (err != nil) != test.wantErr || (got == nil) != test.wantNil {
					t.Fatalf("projection = %+v, error = %v; want nil=%v, error=%v", got, err, test.wantNil, test.wantErr)
				}
				if got != nil && (got.ID != "topup" || got.ProductID != "price") {
					t.Fatalf("top-up projection = %+v", got)
				}
			})
		}
	})
}

func TestPostgresBillingStateRemainingValidationPaths(t *testing.T) {
	ctx := t.Context()
	store := (*PostgresStore)(nil)
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	validID := "00000000-0000-4000-8000-000000000001"

	validSubscription := CommerceSubscription{
		AccountID: validID, Provider: "alpha", ProviderSubscriptionID: "sub", OfferID: validID,
		Status: "active", ProviderUpdatedAt: now,
	}
	for _, mutate := range []func(*CommerceSubscription){
		func(value *CommerceSubscription) { value.AccountID = "" },
		func(value *CommerceSubscription) { value.Provider = "" },
		func(value *CommerceSubscription) { value.ProviderSubscriptionID = "" },
		func(value *CommerceSubscription) { value.OfferID = "" },
		func(value *CommerceSubscription) { value.Status = "invalid" },
		func(value *CommerceSubscription) { value.ProviderUpdatedAt = time.Time{} },
		func(value *CommerceSubscription) { value.GraceEndsAt = &now },
		func(value *CommerceSubscription) { value.AccountID = "bad" },
		func(value *CommerceSubscription) { value.OfferID = "bad" },
		func(value *CommerceSubscription) { value.Metadata = map[string]any{"bad": make(chan int)} },
	} {
		value := validSubscription
		mutate(&value)
		if _, err := store.UpsertBillingSubscriptionState(ctx, value); err == nil {
			t.Fatalf("invalid subscription was accepted: %+v", value)
		}
	}

	for _, test := range []struct {
		name string
		call func() error
	}{
		{"refund payment", func() error {
			_, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "alpha"})
			return err
		}},
		{"refund provider ID", func() error {
			_, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "alpha", ProviderPaymentID: "payment"})
			return err
		}},
		{"refund account", func() error {
			_, err := store.UpsertBillingRefundState(ctx, BillingRefundUpsert{Provider: "alpha", ProviderPaymentID: "payment", ProviderRefundID: "refund"})
			return err
		}},
		{"refund grant", func() error { _, err := store.PostBillingRefund(ctx, validID, "", 1, "key"); return err }},
		{"refund ID", func() error { _, err := store.PostBillingRefund(ctx, "bad", validID, 1, "key"); return err }},
		{"refund grant ID", func() error { _, err := store.PostBillingRefund(ctx, validID, "bad", 1, "key"); return err }},
		{"invoice ID", func() error { return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "alpha"}) }},
		{"invoice account", func() error {
			return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "alpha", ProviderInvoiceID: "invoice"})
		}},
		{"invoice account UUID", func() error {
			return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "alpha", ProviderInvoiceID: "invoice", AccountID: "bad", Status: "open", Currency: "USD", ProviderUpdatedAt: now})
		}},
		{"invoice metadata", func() error {
			return store.UpsertBillingInvoiceState(ctx, BillingInvoiceUpsert{Provider: "alpha", ProviderInvoiceID: "invoice", AccountID: validID, Status: "open", Currency: "USD", ProviderUpdatedAt: now, Metadata: map[string]any{"bad": make(chan int)}})
		}},
		{"dispute ID", func() error { return store.UpsertBillingDisputeState(ctx, BillingDisputeUpsert{Provider: "alpha"}) }},
		{"dispute payment", func() error {
			return store.UpsertBillingDisputeState(ctx, BillingDisputeUpsert{Provider: "alpha", ProviderDisputeID: "dispute"})
		}},
		{"conflict duplicate", func() error {
			return store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{Provider: "alpha"})
		}},
		{"conflict account UUID", func() error {
			return store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{Provider: "alpha", DuplicateProviderSubscriptionID: "duplicate", AccountID: "bad"})
		}},
		{"conflict metadata", func() error {
			return store.RecordSubscriptionConflict(ctx, BillingSubscriptionConflictCreate{Provider: "alpha", DuplicateProviderSubscriptionID: "duplicate", Metadata: map[string]any{"bad": make(chan int)}})
		}},
		{"entitlement account UUID", func() error {
			_, err := store.reconcileSubscriptionEntitlement(ctx, "bad", validID, validID, "active", now, nil, true, "", "subscription_updated")
			return err
		}},
		{"pseudonym UUID", func() error { _, err := store.PseudonymizeFinancialSubject(ctx, "bad"); return err }},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatal("invalid input was accepted")
			}
		})
	}
}

func TestPostgresCommerceStateRemainingValidationPaths(t *testing.T) {
	ctx := t.Context()
	store := (*PostgresStore)(nil)
	validID := "00000000-0000-4000-8000-000000000001"

	for _, test := range []struct {
		name string
		call func() error
	}{
		{"customer account", func() error { return store.UpsertBillingCustomer(ctx, BillingCustomerRecord{}) }},
		{"preferences UUID", func() error { return store.UpsertBillingPreferences(ctx, BillingPreferences{AccountID: "bad"}) }},
		{"top-up key", func() error {
			_, err := store.ResolveAutoRechargeTopup(ctx, AutoRechargeTopupLookup{Provider: "alpha"})
			return err
		}},
		{"profile provider", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: validID, Enabled: true, State: AutoRechargeStateActive}, AutoRechargeProfileUpsertOptions{})
		}},
		{"profile top-up", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: validID, Enabled: true, State: AutoRechargeStateActive, Provider: "alpha"}, AutoRechargeProfileUpsertOptions{})
		}},
		{"profile disabled state", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: validID, State: AutoRechargeStatePaused}, AutoRechargeProfileUpsertOptions{})
		}},
		{"profile UUID", func() error {
			return store.UpsertAutoRechargeProfile(ctx, AutoRechargeProfile{UserID: "bad", State: AutoRechargeStateDisabled}, AutoRechargeProfileUpsertOptions{})
		}},
		{"attempt UUID", func() error {
			_, err := store.ClaimAutoRechargeAttempt(ctx, AutoRechargeAttemptClaim{UserID: "bad", IdempotencyKey: "key"})
			return err
		}},
		{"count UUID", func() error { _, err := store.CountAutoRechargeAttempts(ctx, "bad", time.Now()); return err }},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := test.call(); err == nil {
				t.Fatal("invalid input was accepted")
			}
		})
	}
}
