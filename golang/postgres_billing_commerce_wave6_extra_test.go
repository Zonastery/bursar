package bursar

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
	"time"
)

func TestPostgresBillingClaimAndAccountBoundaries(t *testing.T) {
	now := time.Date(2026, 8, 19, 1, 2, 3, 0, time.UTC)
	base := BillingEvent{EventID: "evt_1", Provider: "stripe", Type: BillingEventPaymentSucceeded, OccurredAt: now, AccountID: storageTestTenant,
		Payment: &BillingPayment{ProviderPaymentID: "pay_1", AmountMinor: 1, Currency: "USD", Purpose: "subscription", Status: "succeeded"}}
	for _, event := range []BillingEvent{
		{},
		{EventID: "evt", Provider: "stripe", Type: BillingEventPaymentSucceeded, OccurredAt: now},
		{EventID: "evt", Provider: "stripe", Type: BillingEventPaymentSucceeded, OccurredAt: now, Payment: &BillingPayment{}},
	} {
		if err := event.Validate(); err == nil {
			t.Errorf("invalid billing event accepted: %#v", event)
		}
	}
	envelope := billingEventClaimEnvelope(BillingEvent{
		EventID: "evt", Provider: " stripe ", Type: BillingEventPaymentSucceeded, OccurredAt: now,
		AccountID: storageTestTenant, Customer: &BillingCustomer{ProviderCustomerID: "cus", Email: " user@example.test "},
	})
	if envelope["provider"] != "stripe" || envelope["accountId"] != storageTestTenant || envelope["eventId"] != "evt" {
		t.Fatalf("claim envelope = %#v", envelope)
	}
	customer, ok := envelope["customer"].(map[string]any)
	if !ok || customer["providerCustomerId"] != "cus" || customer["email"] != "user@example.test" {
		t.Fatalf("customer envelope = %#v", envelope["customer"])
	}

	store := &PostgresStore{client: &PostgresClient{}}
	var nilStore *PostgresStore
	if _, err := nilStore.ClaimBillingEvent(context.Background(), base, nil); err == nil {
		t.Fatal("nil billing store claimed an event")
	}
	if _, err := store.ClaimBillingEvent(context.Background(), base, map[string]any{"bad": func() {}}); err == nil {
		t.Fatal("non-serializable claim envelope accepted")
	}
	if _, err := store.ClaimBillingEvent(context.Background(), BillingEvent{}, nil); err == nil {
		t.Fatal("invalid claim event accepted")
	}
	if _, err := store.CompleteBillingEvent(context.Background(), "stripe", "evt", storageTestClaim); err == nil {
		t.Fatal("closed transaction completion succeeded")
	}
	if _, err := store.FailBillingEvent(context.Background(), "stripe", "evt", storageTestClaim, strings.Repeat("x", 9_000)); err == nil {
		t.Fatal("closed transaction failure succeeded")
	}
	for _, operation := range []struct {
		name string
		run  func() error
	}{
		{"complete nil store", func() error {
			_, err := nilStore.CompleteBillingEvent(context.Background(), "stripe", "evt", storageTestClaim)
			return err
		}},
		{"complete missing provider", func() error {
			_, err := store.CompleteBillingEvent(context.Background(), " ", "evt", storageTestClaim)
			return err
		}},
		{"complete missing event", func() error {
			_, err := store.CompleteBillingEvent(context.Background(), "stripe", " ", storageTestClaim)
			return err
		}},
		{"complete missing token", func() error {
			_, err := store.CompleteBillingEvent(context.Background(), "stripe", "evt", " ")
			return err
		}},
		{"complete malformed token", func() error {
			_, err := store.CompleteBillingEvent(context.Background(), "stripe", "evt", "not-a-uuid")
			return err
		}},
		{"fail nil store", func() error {
			_, err := nilStore.FailBillingEvent(context.Background(), "stripe", "evt", storageTestClaim, "failed")
			return err
		}},
		{"fail missing provider", func() error {
			_, err := store.FailBillingEvent(context.Background(), " ", "evt", storageTestClaim, "failed")
			return err
		}},
		{"fail missing event", func() error {
			_, err := store.FailBillingEvent(context.Background(), "stripe", " ", storageTestClaim, "failed")
			return err
		}},
		{"fail missing token", func() error {
			_, err := store.FailBillingEvent(context.Background(), "stripe", "evt", " ", "failed")
			return err
		}},
		{"fail malformed token", func() error {
			_, err := store.FailBillingEvent(context.Background(), "stripe", "evt", "not-a-uuid", "failed")
			return err
		}},
	} {
		t.Run(operation.name, func(t *testing.T) {
			if err := operation.run(); err == nil {
				t.Fatal("invalid billing event operation succeeded")
			}
		})
	}
	for _, event := range []BillingEvent{
		{AccountID: storageTestTenant},
		{Metadata: map[string]any{"account_id": storageTestTenant}},
		{Customer: &BillingCustomer{AccountID: storageTestTenant}},
		{Subscription: &BillingSubscription{AccountID: storageTestTenant}},
		{Invoice: &BillingInvoice{Metadata: map[string]any{"account_id": storageTestTenant}}},
		{Payment: &BillingPayment{Metadata: map[string]any{"account_id": storageTestTenant}}},
		{Metadata: map[string]any{"bursar_account_id": storageTestTenant}, Customer: &BillingCustomer{AccountID: storageOtherTenant}},
	} {
		account, err := store.ResolveBillingEventAccount(context.Background(), event)
		if err != nil || account != storageTestTenant {
			t.Fatalf("account resolution %#v = %q, %v", event, account, err)
		}
	}
	if account, err := store.ResolveBillingEventAccount(context.Background(), BillingEvent{}); err != nil || account != "" {
		t.Fatalf("empty account resolution = %q, %v", account, err)
	}
	for _, event := range []BillingEvent{
		{Provider: "stripe", Customer: &BillingCustomer{ProviderCustomerID: "cus_lookup"}},
		{Provider: "stripe", Subscription: &BillingSubscription{ProviderSubscriptionID: "sub_lookup"}},
		{Provider: "stripe", Payment: &BillingPayment{ProviderPaymentID: "pay_lookup"}},
		{Provider: "stripe", Refund: &BillingRefund{ProviderPaymentID: "pay_lookup"}},
		{Provider: "stripe", Dispute: &BillingDispute{ProviderPaymentID: "pay_lookup"}},
	} {
		if _, err := store.ResolveBillingEventAccount(context.Background(), event); err == nil {
			t.Errorf("closed account lookup unexpectedly succeeded: %#v", event)
		}
	}
}

func TestPostgresCommerceCheckoutDigestAndScalarBoundaries(t *testing.T) {
	input := CheckoutIntentCreate{AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "credit_topup", ProductKey: "starter", Quantity: 2, IdempotencyKey: "key"}
	digest, err := checkoutRequestDigest(input)
	if err != nil {
		t.Fatal(err)
	}
	if hex.EncodeToString(digest[:]) == "" {
		t.Fatal("checkout digest was empty")
	}
	topupDigest, err := checkoutRequestDigest(input)
	if err != nil || digest != topupDigest {
		t.Fatal("checkout digest was not deterministic")
	}
	changed := input
	changed.Quantity++
	changedDigest, err := checkoutRequestDigest(changed)
	if err != nil || digest == changedDigest {
		t.Fatal("checkout quantity was not digest-bound")
	}
	for _, value := range []any{digest[:], hex.EncodeToString(digest[:])} {
		got, err := checkoutRowDigest(map[string]any{"request_digest": value})
		if err != nil || got != hex.EncodeToString(digest[:]) {
			t.Errorf("checkout row digest %T = %q, %v", value, got, err)
		}
	}
	for _, value := range []any{nil, []byte("short"), "not-hex", 1} {
		if _, err := checkoutRowDigest(map[string]any{"request_digest": value}); err == nil {
			t.Errorf("invalid checkout digest %#v accepted", value)
		}
	}
	if _, err := checkoutRowDigest(map[string]any{}); err == nil {
		t.Fatal("missing checkout digest accepted")
	}
	subscriptionInput := input
	subscriptionInput.CheckoutKind = "subscription"
	if subscriptionDigest, err := checkoutRequestDigest(subscriptionInput); err != nil || subscriptionDigest == digest {
		t.Fatalf("subscription checkout digest = %x, %v", subscriptionDigest, err)
	}
	for _, value := range []any{"intent", []byte("intent"), "  intent  "} {
		if got, err := textFromScalar(value, "intent"); err != nil || got != "intent" {
			t.Errorf("text scalar %#v = %q, %v", value, got, err)
		}
	}
	for _, value := range []any{nil, " ", []byte(" ")} {
		if _, err := textFromScalar(value, "intent"); err == nil {
			t.Errorf("invalid scalar %#v accepted", value)
		}
	}

	store := &PostgresStore{client: &PostgresClient{}}
	var nilStore *PostgresStore
	if _, err := nilStore.CreateOrGetCheckoutIntent(context.Background(), CheckoutIntentCreate{}); err == nil {
		t.Fatal("nil commerce store created checkout")
	}
	if err := nilStore.UpdateCheckoutIntent(context.Background(), storageTestClaim, storageTestTenant, CheckoutIntentUpdate{}); err == nil {
		t.Fatal("nil commerce store updated checkout")
	}
	if _, err := nilStore.GetCheckoutIntent(context.Background(), storageTestClaim, storageTestTenant); err == nil {
		t.Fatal("nil commerce store read checkout")
	}
	valid := CheckoutIntentCreate{
		SubjectID: storageTestTenant, AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription",
		ProductKey: "starter", Quantity: 1, IdempotencyKey: "checkout-key", ExpiresAt: time.Now().Add(time.Hour),
	}
	for _, input := range []CheckoutIntentCreate{
		{AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", ProductKey: "starter", Quantity: 1, IdempotencyKey: "key"},
		{SubjectID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", ProductKey: "starter", Quantity: 1, IdempotencyKey: "key"},
		{SubjectID: storageTestTenant, AccountID: storageTestTenant, CheckoutKind: "subscription", ProductKey: "starter", Quantity: 1, IdempotencyKey: "key"},
		{SubjectID: storageTestTenant, AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", ProductKey: "starter", Quantity: 1},
		{SubjectID: storageTestTenant, AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "bad", ProductKey: "starter", Quantity: 1, IdempotencyKey: "key"},
		{SubjectID: storageTestTenant, AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", Quantity: 1, IdempotencyKey: "key"},
		{SubjectID: storageTestTenant, AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", ProductKey: "starter", Quantity: 0, IdempotencyKey: "key"},
		{SubjectID: storageTestTenant, AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", ProductKey: "starter", Quantity: 1, IdempotencyKey: "key", ExpiresAt: time.Now().Add(-time.Hour)},
		{SubjectID: "not-a-uuid", AccountID: storageTestTenant, Provider: "stripe", CheckoutKind: "subscription", ProductKey: "starter", Quantity: 1, IdempotencyKey: "key", ExpiresAt: time.Now().Add(time.Hour)},
	} {
		if _, err := store.CreateOrGetCheckoutIntent(context.Background(), input); err == nil {
			t.Errorf("invalid checkout input accepted: %#v", input)
		}
	}
	conflict := valid
	conflict.RequestDigest = strings.Repeat("0", sha256.Size*2)
	if _, err := store.CreateOrGetCheckoutIntent(context.Background(), conflict); err == nil {
		t.Fatal("conflicting checkout digest accepted")
	}
	zeroExpiry := valid
	zeroExpiry.ExpiresAt = time.Time{}
	if _, err := store.CreateOrGetCheckoutIntent(context.Background(), zeroExpiry); err == nil {
		t.Fatal("checkout unexpectedly persisted without a database")
	}
	for _, input := range []struct {
		intentID  string
		subjectID string
		update    CheckoutIntentUpdate
	}{
		{"", storageTestTenant, CheckoutIntentUpdate{}},
		{storageTestClaim, "", CheckoutIntentUpdate{}},
		{storageTestClaim, storageTestTenant, CheckoutIntentUpdate{Status: "unsupported"}},
		{"not-a-uuid", storageTestTenant, CheckoutIntentUpdate{}},
	} {
		if err := store.UpdateCheckoutIntent(context.Background(), input.intentID, input.subjectID, input.update); err == nil {
			t.Errorf("invalid checkout transition accepted: %#v", input)
		}
	}
	for _, input := range [][2]string{{"", storageTestTenant}, {storageTestClaim, ""}} {
		if _, err := store.GetCheckoutIntent(context.Background(), input[0], input[1]); err == nil {
			t.Errorf("invalid checkout lookup succeeded: %#v", input)
		}
	}
	tx := &PostgresTransaction{}
	if _, err := store.checkoutIntent(context.Background(), tx, "not-a-uuid", storageTestTenant); err == nil {
		t.Fatal("malformed checkout intent ID accepted")
	}
	if _, err := store.checkoutIntent(context.Background(), tx, storageTestClaim, "not-a-uuid"); err == nil {
		t.Fatal("malformed checkout subject ID accepted")
	}
}

func TestCheckoutIntentProjectionRejectsIncompleteFinancialRows(t *testing.T) {
	now := time.Date(2026, 8, 19, 4, 5, 6, 0, time.UTC)
	valid := map[string]any{
		"id": "00000000-0000-4000-8000-000000000301", "subject_id": storageTestTenant,
		"provider": "stripe", "checkout_kind": "subscription", "product_key": "pro_month",
		"status": "open", "expires_at": now.Add(time.Hour), "created_at": now, "updated_at": now,
		"request_digest": make([]byte, sha256.Size),
	}
	intent, err := checkoutIntentFromRows([]map[string]any{valid})
	if err != nil || intent == nil || intent.ProductKey != "pro_month" {
		t.Fatalf("valid checkout projection = %+v, %v", intent, err)
	}
	if missing, err := checkoutIntentFromRows(nil); err != nil || missing != nil {
		t.Fatalf("empty checkout projection = %+v, %v", missing, err)
	}
	if _, err := checkoutIntentFromRows([]map[string]any{nil}); err == nil {
		t.Fatal("nil checkout projection row was accepted")
	}

	for _, key := range []string{
		"id", "subject_id", "provider", "checkout_kind", "product_key", "status",
		"expires_at", "created_at", "updated_at", "request_digest",
	} {
		t.Run(key, func(t *testing.T) {
			row := cloneAnyMap(valid)
			delete(row, key)
			if _, err := checkoutIntentFromRows([]map[string]any{row}); err == nil {
				t.Fatalf("checkout projection missing %s was accepted", key)
			}
		})
	}
}

func TestCheckoutIntentIdentifierAcceptsDriverScalarNames(t *testing.T) {
	const intentID = "00000000-0000-4000-8000-000000000301"
	for name, rows := range map[string][]map[string]any{
		"canonical": {{"create_checkout_intent": intentID}},
		"driver":    {{"unnamed_scalar": intentID}},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := checkoutIntentIDFromRows(rows)
			if err != nil || got != intentID {
				t.Fatalf("checkout intent ID = %q, %v", got, err)
			}
		})
	}
	for name, rows := range map[string][]map[string]any{
		"missing row":      nil,
		"nil row":          {nil},
		"multiple columns": {{"first": intentID, "second": intentID}},
		"blank scalar":     {{"unnamed_scalar": " "}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := checkoutIntentIDFromRows(rows); err == nil {
				t.Fatal("invalid checkout intent result was accepted")
			}
		})
	}
}

func TestPostgresCommerceAndBillingProjectionRowsFailClosed(t *testing.T) {
	if _, err := firstScalar(map[string]any{"result": true, "other": false}, "scalar"); err == nil {
		t.Fatal("multi-column scalar result accepted")
	}
	for _, event := range []BillingEvent{
		{EventID: "evt", Provider: "stripe", Type: BillingEventSubscriptionUpdated, OccurredAt: time.Now(), Subscription: &BillingSubscription{ProviderSubscriptionID: "sub", Status: "unsupported"}},
		{EventID: "evt", Provider: "stripe", Type: BillingEventDisputeCreated, OccurredAt: time.Now(), Dispute: &BillingDispute{ProviderDisputeID: "d", ProviderPaymentID: "p", Status: "unsupported"}},
	} {
		if err := event.Validate(); err == nil {
			t.Errorf("invalid lifecycle event accepted: %#v", event)
		}
	}

	valid := map[string]any{"provider": "stripe", "provider_invoice_id": "in_1", "subject_id": storageTestTenant, "status": "paid", "currency": "USD", "amount_paid_minor": int64(10), "amount_due_minor": int64(0), "metadata": []byte(`{"invoice":"ok"}`)}
	if invoice, err := commerceStateInvoiceFromRow(valid, "invoice"); err != nil || invoice.Metadata["invoice"] != "ok" {
		t.Fatalf("invoice JSON projection = %#v, %v", invoice, err)
	}
	for _, key := range []string{"provider_invoice_id", "subject_id", "status", "currency", "amount_paid_minor", "amount_due_minor"} {
		row := cloneAnyMap(valid)
		delete(row, key)
		if _, err := commerceStateInvoiceFromRow(row, "invoice"); err == nil {
			t.Errorf("invoice missing %s accepted", key)
		}
	}

	for _, code := range []string{"idempotency_conflict", "open_change_exists", "missing_subscription", "invalid_target_offer", "invalid_request", "unknown"} {
		err := commerceStateSubscriptionChangeRejected(code)
		if err == nil {
			t.Fatalf("change rejection %q returned nil", code)
		}
	}
}
