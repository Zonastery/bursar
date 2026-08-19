package bursar

import (
	"context"
	"testing"
	"time"
)

func TestCommerceStateStatusAndLookupBoundaries(t *testing.T) {
	for _, status := range []string{"incomplete", "incomplete_expired", "trialing", "active", "past_due", "canceled", "unpaid", "paused", "expired"} {
		if !commerceStateSubscriptionStatus(status) {
			t.Errorf("subscription status %q rejected", status)
		}
	}
	if commerceStateSubscriptionStatus("unknown") {
		t.Fatal("unknown subscription status accepted")
	}
	for _, statuses := range [][]string{nil, {}, {"active", " past_due "}} {
		allowed, err := commerceStateSubscriptionStatusSet(statuses)
		if err != nil || (statuses != nil && len(allowed) != len(statuses)) {
			t.Fatalf("status set %#v = %#v, %v", statuses, allowed, err)
		}
	}
	if _, err := commerceStateSubscriptionStatusSet([]string{"unknown"}); err == nil {
		t.Fatal("unknown status set accepted")
	}

	for _, tc := range []struct {
		product, price, lookup string
		wantType, wantValue    string
	}{
		{"product", "price", "external", "price_id", "price"},
		{"product", "", "external", "product_id", "product"},
		{"", "", "external", "external_id", "external"},
		{"", "", "", "", ""},
	} {
		gotType, gotValue := commerceStateOfferLookup(tc.product, tc.price, tc.lookup)
		if gotType != tc.wantType || gotValue != tc.wantValue {
			t.Errorf("offer lookup = %q/%q, want %q/%q", gotType, gotValue, tc.wantType, tc.wantValue)
		}
	}
	product, price, external := "prod", "price", "external"
	for _, tc := range []struct {
		name string
		ref  ProviderReference
		kind string
		want string
	}{
		{"price", ProviderReference{PriceID: &price, ProductID: &product, ExternalID: &external}, "price_id", "price"},
		{"product", ProviderReference{ProductID: &product, ExternalID: &external}, "product_id", "prod"},
		{"external", ProviderReference{ExternalID: &external}, "external_id", "external"},
		{"empty", ProviderReference{}, "", ""},
	} {
		t.Run("provider/"+tc.name, func(t *testing.T) {
			kind, value := commerceStateProviderReferenceLookup(tc.ref)
			if kind != tc.kind || value != tc.want {
				t.Fatalf("provider lookup = %q/%q, want %q/%q", kind, value, tc.kind, tc.want)
			}
		})
	}
	for _, tc := range []struct {
		value string
		ok    bool
	}{
		{"1", true}, {" 2 ", true}, {"0", false}, {"-1", false}, {"bad", false},
	} {
		got, err := commerceStateSubscriptionChangeID(tc.value)
		if tc.ok && (err != nil || got < 1) {
			t.Errorf("change ID %q = %d, %v", tc.value, got, err)
		}
		if !tc.ok && err == nil {
			t.Errorf("change ID %q accepted", tc.value)
		}
	}
	for _, state := range []string{"awaiting_payment", "scheduled", "applied", "failed", "canceled"} {
		if !commerceStateSubscriptionChangeState(state) {
			t.Errorf("change state %q rejected", state)
		}
	}
	if commerceStateSubscriptionChangeState("unknown") {
		t.Fatal("unknown change state accepted")
	}
	for _, code := range []string{"idempotency_conflict", "open_change_exists", "missing_subscription", "invalid_target_offer", "invalid_request"} {
		if err := commerceStateSubscriptionChangeRejected(code); err == nil {
			t.Errorf("rejection %q returned nil", code)
		}
	}
	if err := commerceStateSubscriptionChangeRejected("future_code"); err == nil {
		t.Fatal("unknown rejection returned nil")
	}
}

func TestBillingStateRowMappersRejectMalformedFinancialData(t *testing.T) {
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	payment := map[string]any{
		"id": "payment", "provider": "stripe", "provider_payment_id": "pi_1", "subject_id": storageTestTenant,
		"amount_minor": int64(1234), "tax_minor": int64(34), "currency": "USD", "purpose": "subscription", "status": "succeeded",
		"provider_updated_at": now, "metadata": `{"source":"webhook"}`, "provider_invoice_id": nil,
	}
	mapped, err := billingPaymentRecordFromRow(payment, "payment")
	if err != nil || mapped.AmountMinor != 1234 || mapped.TaxMinor != 34 || mapped.Currency != "USD" || mapped.Metadata["source"] != "webhook" {
		t.Fatalf("payment = %#v, %v", mapped, err)
	}
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"negative amount", func(row map[string]any) { row["amount_minor"] = int64(-1) }},
		{"negative tax", func(row map[string]any) { row["tax_minor"] = int64(-1) }},
		{"currency", func(row map[string]any) { row["currency"] = "US" }},
		{"purpose", func(row map[string]any) { row["purpose"] = "other" }},
		{"status", func(row map[string]any) { row["status"] = "unknown" }},
		{"timestamp", func(row map[string]any) { row["provider_updated_at"] = "bad" }},
		{"metadata", func(row map[string]any) { row["metadata"] = "[]" }},
	} {
		t.Run("payment/"+tc.name, func(t *testing.T) {
			row := cloneAnyMap(payment)
			tc.mutate(row)
			if _, err := billingPaymentRecordFromRow(row, "payment"); err == nil {
				t.Fatal("malformed payment accepted")
			}
		})
	}

	invoice := map[string]any{
		"provider": "stripe", "provider_invoice_id": "in_1", "subject_id": storageTestTenant, "status": "paid", "currency": "usd",
		"amount_paid_minor": int64(100), "amount_due_minor": int64(0), "period_start": now, "period_end": now.Add(time.Hour), "metadata": map[string]any{"x": true},
	}
	mappedInvoice, err := commerceStateInvoiceFromRow(invoice, "invoice")
	if err != nil || mappedInvoice.ID != "in_1" || mappedInvoice.Currency != "USD" || mappedInvoice.AmountPaidMinor != 100 || mappedInvoice.PeriodStart == nil {
		t.Fatalf("invoice = %#v, %v", mappedInvoice, err)
	}
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"status", func(row map[string]any) { row["status"] = "pending" }},
		{"currency", func(row map[string]any) { row["currency"] = "US" }},
		{"paid", func(row map[string]any) { row["amount_paid_minor"] = int64(-1) }},
		{"due", func(row map[string]any) { row["amount_due_minor"] = int64(-1) }},
		{"metadata", func(row map[string]any) { row["metadata"] = "[]" }},
	} {
		t.Run("invoice/"+tc.name, func(t *testing.T) {
			row := cloneAnyMap(invoice)
			tc.mutate(row)
			if _, err := commerceStateInvoiceFromRow(row, "invoice"); err == nil {
				t.Fatal("malformed invoice accepted")
			}
		})
	}

	customer, err := commerceStateCustomerFromRow(map[string]any{"provider": "stripe", "provider_customer_id": "cus_1", "subject_id": storageTestTenant, "email": nil}, "customer")
	if err != nil || customer.AccountID != storageTestTenant || customer.Email != "" {
		t.Fatalf("customer = %#v, %v", customer, err)
	}
	for _, key := range []string{"provider", "provider_customer_id", "subject_id"} {
		row := map[string]any{"provider": "stripe", "provider_customer_id": "cus_1", "subject_id": storageTestTenant}
		delete(row, key)
		if _, err := commerceStateCustomerFromRow(row, "customer"); err == nil {
			t.Errorf("customer missing %s accepted", key)
		}
	}

	preferences, err := commerceStateBillingPreferencesFromRow(map[string]any{"subject_id": storageTestTenant, "auto_recharge": true, "overage_protection": false, "email_notifications": "true", "usage_alerts": []byte("false"), "invoice_reminders": true}, "preferences")
	if err != nil || !preferences.AutoRecharge || preferences.OverageProtection || !preferences.EmailNotifications || preferences.UsageAlerts || !preferences.InvoiceReminders {
		t.Fatalf("preferences = %#v, %v", preferences, err)
	}
	if _, err := commerceStateBillingPreferencesFromRow(map[string]any{"subject_id": storageTestTenant, "auto_recharge": "sometimes", "overage_protection": false, "email_notifications": false, "usage_alerts": false, "invoice_reminders": false}, "preferences"); err == nil {
		t.Fatal("invalid preference boolean accepted")
	}
}

func TestBillingStateInputAndTransitionValidation(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	store := (*PostgresStore)(nil)
	invalidSubscription := CommerceSubscription{AccountID: storageTestTenant, Provider: "stripe", ProviderSubscriptionID: "sub", OfferID: storageTestBilling, Status: "unknown", ProviderUpdatedAt: now}
	if _, err := store.UpsertBillingSubscriptionState(ctx, invalidSubscription); err == nil {
		t.Fatal("invalid subscription status accepted")
	}
	invalidSubscription.Status = "active"
	if _, err := store.UpsertBillingSubscriptionState(ctx, invalidSubscription); err == nil {
		t.Fatal("subscription without catalog revision accepted")
	}
	invalidSubscription.OfferID = storageTestBilling
	invalidSubscription.ProviderUpdatedAt = time.Time{}
	if _, err := store.UpsertBillingSubscriptionState(ctx, invalidSubscription); err == nil {
		t.Fatal("subscription without provider timestamp accepted")
	}
	grace := now
	validFields := CommerceSubscription{AccountID: storageTestTenant, Provider: "stripe", ProviderSubscriptionID: "sub", OfferID: storageTestBilling, Status: "active", ProviderUpdatedAt: now, GraceEndsAt: &grace}
	if _, err := store.UpsertBillingSubscriptionState(ctx, validFields); err == nil {
		t.Fatal("grace deadline on active subscription accepted")
	}

	for _, input := range []BillingPaymentUpsert{
		{AccountID: storageTestTenant, Provider: "stripe", ProviderPaymentID: "pi", AmountMinor: -1, Currency: "USD", Purpose: "subscription", Status: "succeeded", ProviderUpdatedAt: now},
		{AccountID: storageTestTenant, Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "other", Status: "succeeded", ProviderUpdatedAt: now},
		{AccountID: storageTestTenant, Provider: "stripe", ProviderPaymentID: "pi", Currency: "USD", Purpose: "subscription", Status: "succeeded"},
	} {
		if _, err := store.UpsertBillingPaymentState(ctx, input); err == nil {
			t.Errorf("invalid payment input accepted: %#v", input)
		}
	}
	if err := store.UpdateBillingSubscriptionChange(ctx, "1", BillingSubscriptionChangeUpdate{}); err != nil {
		t.Fatalf("state-less change update = %v", err)
	}
	for _, tc := range []struct {
		id    string
		state string
	}{
		{"0", "applied"}, {"bad", "applied"}, {"1", "unknown"},
	} {
		state := tc.state
		if err := store.UpdateBillingSubscriptionChange(ctx, tc.id, BillingSubscriptionChangeUpdate{State: &state}); err == nil {
			t.Errorf("invalid change update accepted: %#v", tc)
		}
	}
}
