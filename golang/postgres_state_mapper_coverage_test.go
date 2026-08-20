package bursar

import (
	"context"
	"testing"
	"time"
)

func TestPostgresStateScalarAndFinancialMapperBoundaries(t *testing.T) {
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	for _, tc := range []struct {
		name  string
		value any
		want  int64
	}{
		{"int", int(1), 1},
		{"int8", int8(2), 2},
		{"int16", int16(3), 3},
		{"int32", int32(4), 4},
		{"int64", int64(5), 5},
		{"uint", uint(6), 6},
		{"uint8", uint8(7), 7},
		{"uint16", uint16(8), 8},
		{"uint32", uint32(9), 9},
		{"uint64", uint64(10), 10},
		{"string", "11", 11},
		{"bytes", []byte("12"), 12},
	} {
		t.Run("scalar/"+tc.name, func(t *testing.T) {
			if got, err := commerceStateRowInt64(map[string]any{"value": tc.value}, "value", "scalar"); err != nil || got != tc.want {
				t.Fatalf("scalar %T = %d, %v; want %d", tc.value, got, err, tc.want)
			}
		})
	}
	for _, value := range []any{nil, true, uint64(^uint64(0)>>1) + 1, "bad", []byte("bad")} {
		if _, err := commerceStateRowInt64(map[string]any{"value": value}, "value", "scalar"); err == nil {
			t.Errorf("invalid scalar %T accepted", value)
		}
	}
	if value, err := commerceStateOptionalRowInt64(map[string]any{}, "quoted_amount_minor", "optional"); err != nil || value != nil {
		t.Fatalf("missing optional scalar = %v, %v", value, err)
	}

	payment := map[string]any{
		"id": "payment", "provider": "stripe", "provider_payment_id": "pi_1", "subject_id": storageTestTenant,
		"amount_minor": int64(1234), "tax_minor": int64(34), "currency": "USD", "purpose": "subscription", "status": "succeeded",
		"provider_updated_at": now, "metadata": `{"source":"mapper"}`,
	}
	for _, key := range []string{"id", "provider", "provider_payment_id", "subject_id", "amount_minor", "tax_minor", "currency", "purpose", "status", "provider_updated_at"} {
		row := cloneAnyMap(payment)
		delete(row, key)
		if _, err := billingPaymentRecordFromRow(row, "payment"); err == nil {
			t.Errorf("payment missing %s accepted", key)
		}
	}

	invoice := map[string]any{
		"provider": "stripe", "provider_invoice_id": "in_1", "subject_id": storageTestTenant, "status": "paid", "currency": "USD",
		"amount_paid_minor": int64(100), "amount_due_minor": int64(20), "period_start": now, "period_end": now.Add(time.Hour), "metadata": []byte(`{"source":"mapper"}`),
	}
	if mapped, err := commerceStateInvoiceFromRow(invoice, "invoice"); err != nil || mapped.PeriodEnd == nil || mapped.Metadata["source"] != "mapper" {
		t.Fatalf("invoice optional fields = %#v, %v", mapped, err)
	}
	for _, key := range []string{"provider", "provider_invoice_id", "subject_id", "status", "currency", "amount_paid_minor", "amount_due_minor"} {
		row := cloneAnyMap(invoice)
		delete(row, key)
		if _, err := commerceStateInvoiceFromRow(row, "invoice"); err == nil {
			t.Errorf("invoice missing %s accepted", key)
		}
	}
	for _, key := range []string{"period_start", "period_end"} {
		row := cloneAnyMap(invoice)
		row[key] = "bad"
		if _, err := commerceStateInvoiceFromRow(row, "invoice"); err == nil {
			t.Errorf("invoice malformed %s accepted", key)
		}
	}

	preferences := map[string]any{
		"subject_id": storageTestTenant, "auto_recharge": true, "overage_protection": false,
		"email_notifications": true, "usage_alerts": false, "invoice_reminders": true,
	}
	for _, key := range []string{"subject_id", "auto_recharge", "overage_protection", "email_notifications", "usage_alerts", "invoice_reminders"} {
		row := cloneAnyMap(preferences)
		delete(row, key)
		if _, err := commerceStateBillingPreferencesFromRow(row, "preferences"); err == nil {
			t.Errorf("preferences missing %s accepted", key)
		}
	}

	profile := map[string]any{
		"subject_id": storageTestTenant, "enabled": true, "armed": true, "state": string(AutoRechargeStateActive),
		"provider": "stripe", "topup_id": storageTestTenant, "quantity": int64(2), "threshold": "1.25",
		"max_charges_per_window": int64(3), "window_unit": "month", "window_count": int64(1), "window_anchor": "calendar",
		"window_timezone": "UTC", "updated_at": now,
	}
	for _, key := range []string{"subject_id", "enabled", "armed", "state", "updated_at"} {
		row := cloneAnyMap(profile)
		delete(row, key)
		if _, err := commerceStateAutoRechargeProfileFromRow(row, "profile"); err == nil {
			t.Errorf("profile missing %s accepted", key)
		}
	}
	for _, key := range []string{"quantity", "max_charges_per_window", "window_count"} {
		row := cloneAnyMap(profile)
		row[key] = "bad"
		if _, err := commerceStateAutoRechargeProfileFromRow(row, "profile"); err == nil {
			t.Errorf("profile malformed %s accepted", key)
		}
	}
	row := cloneAnyMap(profile)
	row["threshold"] = "bad"
	if _, err := commerceStateAutoRechargeProfileFromRow(row, "profile"); err == nil {
		t.Fatal("profile malformed threshold accepted")
	}
	if mapped, err := commerceStateAutoRechargeProfileFromRow(map[string]any{
		"subject_id": storageTestTenant, "enabled": false, "armed": true, "state": string(AutoRechargeStateDisabled), "updated_at": now,
	}, "profile"); err != nil || mapped.Enabled {
		t.Fatalf("minimal disabled profile = %#v, %v", mapped, err)
	}

	attempt := map[string]any{
		"id": storageTestTenant, "subject_id": storageTestTenant, "provider": "stripe", "idempotency_key": "attempt-key",
		"provider_attempt_id": "provider-attempt", "topup_id": storageTestTenant, "quantity": int64(2),
		"state": string(AutoRechargeAttemptSucceeded), "window_start": now, "window_end": now.Add(time.Hour),
		"quoted_amount_minor": int64(250), "currency": "USD", "metadata": `{"source":"mapper"}`,
		"created_at": now, "updated_at": now,
	}
	for _, key := range []string{"id", "subject_id", "provider", "idempotency_key", "topup_id", "quantity", "state", "window_start", "window_end", "created_at", "updated_at"} {
		row := cloneAnyMap(attempt)
		delete(row, key)
		if _, err := commerceStateAutoRechargeAttemptFromRow(row, "attempt"); err == nil {
			t.Errorf("attempt missing %s accepted", key)
		}
	}
	for _, mutate := range []func(map[string]any){
		func(row map[string]any) { row["idempotency_key"] = " " },
		func(row map[string]any) { row["quantity"] = "bad" },
		func(row map[string]any) { row["window_start"] = "bad" },
		func(row map[string]any) { row["window_end"] = "bad" },
		func(row map[string]any) { row["quoted_amount_minor"] = "bad" },
		func(row map[string]any) { row["currency"] = "US" },
		func(row map[string]any) { row["quoted_amount_minor"] = nil; row["currency"] = "USD" },
		func(row map[string]any) { row["quoted_amount_minor"] = int64(1); row["currency"] = nil },
		func(row map[string]any) { row["created_at"] = "bad" },
		func(row map[string]any) { row["updated_at"] = "bad" },
	} {
		row := cloneAnyMap(attempt)
		mutate(row)
		if _, err := commerceStateAutoRechargeAttemptFromRow(row, "attempt"); err == nil {
			t.Errorf("malformed attempt accepted: %#v", row)
		}
	}
	optionalAttempt := cloneAnyMap(attempt)
	optionalAttempt["quoted_amount_minor"], optionalAttempt["currency"] = nil, nil
	if mapped, err := commerceStateAutoRechargeAttemptFromRow(optionalAttempt, "attempt"); err != nil || mapped.QuotedAmountMinor != nil || mapped.Currency != "" {
		t.Fatalf("unquoted attempt = %#v, %v", mapped, err)
	}

	ctx := context.Background()
	if _, _, _, _, _, err := commerceStateOfferContext(ctx, nil, "bad", storageTestBilling, "offer"); err == nil {
		t.Fatal("invalid offer UUID accepted")
	}
	if _, _, _, _, _, err := commerceStateOfferContext(ctx, nil, storageTestBilling, "bad", "offer"); err == nil {
		t.Fatal("invalid revision UUID accepted")
	}

	baseSubscription := map[string]any{
		"id": storageTestBilling, "subject_id": storageTestTenant, "provider": "stripe", "provider_subscription_id": "sub",
		"offer_id": storageTestBilling, "catalog_revision_id": storageTestBilling, "status": "active", "provider_updated_at": now,
		"cancel_at_period_end": false, "metadata": `{}`,
	}
	for _, key := range []string{"id", "subject_id", "provider", "provider_subscription_id", "offer_id", "catalog_revision_id", "status"} {
		row := cloneAnyMap(baseSubscription)
		delete(row, key)
		if _, err := commerceStateSubscriptionFromRow(ctx, nil, row, "subscription"); err == nil {
			t.Errorf("subscription missing %s accepted", key)
		}
	}
	invalidSubscription := cloneAnyMap(baseSubscription)
	invalidSubscription["status"] = "invalid"
	if _, err := commerceStateSubscriptionFromRow(ctx, nil, invalidSubscription, "subscription"); err == nil {
		t.Fatal("invalid subscription status accepted")
	}

	baseOffer := map[string]any{"id": storageTestBilling, "catalog_revision_id": storageTestBilling, "offer_key": "offer", "plan_key": "plan"}
	for _, key := range []string{"id", "catalog_revision_id", "offer_key", "plan_key"} {
		row := cloneAnyMap(baseOffer)
		delete(row, key)
		if _, err := commerceStateBillingOfferFromRow(ctx, nil, row, "stripe", "product", "price", "lookup", "offer"); err == nil {
			t.Errorf("offer missing %s accepted", key)
		}
	}

	baseChange := map[string]any{
		"id": "1", "subscription_id": storageTestBilling, "to_offer_id": storageTestBilling, "to_catalog_revision_id": storageTestBilling,
		"effective_at": now, "effective_behavior": "immediate", "state": "scheduled", "proration_behavior": "none",
		"created_at": now, "updated_at": now, "idempotency_key": "change-key",
	}
	for _, key := range []string{"id", "subscription_id", "to_offer_id", "to_catalog_revision_id"} {
		row := cloneAnyMap(baseChange)
		delete(row, key)
		if _, err := commerceStateSubscriptionChangeFromRow(ctx, nil, row, "stripe", "sub", "change"); err == nil {
			t.Errorf("change missing %s accepted", key)
		}
	}
}
