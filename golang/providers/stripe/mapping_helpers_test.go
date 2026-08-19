// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package stripe

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	stripego "github.com/stripe/stripe-go/v84"
)

func TestStripeMappingHelpersCoverFinancialIdentifiersAndPeriods(t *testing.T) {
	invoice := &stripego.Invoice{
		ID: "in_1", Metadata: map[string]string{"invoice": "value"}, PeriodStart: 10, PeriodEnd: 20,
		TotalTaxes: []*stripego.InvoiceTotalTax{{Amount: 30}},
		Parent: &stripego.InvoiceParent{SubscriptionDetails: &stripego.InvoiceParentSubscriptionDetails{
			Subscription: &stripego.Subscription{ID: "sub_1"}, Metadata: map[string]string{"bursar_account_id": "acct_1"},
		}},
		Payments: &stripego.InvoicePaymentList{Data: []*stripego.InvoicePayment{{Payment: &stripego.InvoicePaymentPayment{PaymentIntent: &stripego.PaymentIntent{ID: "pi_1"}}}}},
	}
	subscription := &bursar.BillingSubscription{}
	start, end := stripeInvoicePeriods(invoice, subscription)
	if start == nil || end == nil || start.Unix() != 10 || end.Unix() != 20 {
		t.Fatalf("invoice periods = %v, %v", start, end)
	}
	if got := stripeInvoiceSubscriptionID(invoice); got != "sub_1" {
		t.Fatalf("subscription ID = %q", got)
	}
	metadata := stripeInvoiceMetadata(invoice)
	if metadata["invoice"] != "value" || metadata["bursar_account_id"] != "acct_1" {
		t.Fatalf("invoice metadata = %#v", metadata)
	}
	if got := stripeInvoicePaymentID(invoice); got != "pi_1" {
		t.Fatalf("payment ID = %q", got)
	}
	if got := stripeInvoicePaymentID(&stripego.Invoice{ID: "fallback"}); got != "fallback" {
		t.Fatalf("invoice fallback payment ID = %q", got)
	}
	periodStart, periodEnd := time.Unix(30, 0).UTC(), time.Unix(40, 0).UTC()
	if start, end := stripeInvoicePeriods(&stripego.Invoice{}, &bursar.BillingSubscription{PeriodStart: &periodStart, PeriodEnd: &periodEnd}); start == nil || end == nil || start.Unix() != 30 || end.Unix() != 40 {
		t.Fatalf("subscription fallback periods = %v, %v", start, end)
	}
	if tax, err := stripeInvoiceTaxMinor(invoice); err != nil || tax != 30 {
		t.Fatalf("invoice tax = %d, err = %v", tax, err)
	}
	if got := stripeChargeID(&stripego.Charge{ID: "ch_1"}); got != "ch_1" || stripeChargeID(nil) != "" {
		t.Fatalf("charge IDs are not normalized")
	}
	if stripeCustomerID(nil) != "" || stripeSubscriptionID(nil) != "" || stripePaymentIntentID(nil) != "" {
		t.Fatal("nil provider identifiers should be empty")
	}
	if got := stripeMetadataAny(map[string]string{"key": "value"}); got["key"] != "value" || stripeMetadataAny(nil) != nil {
		t.Fatalf("metadata projection = %#v", got)
	}
	if info, err := stripeSubscriptionInfo(&stripego.Subscription{ID: "sub_1", Status: stripego.SubscriptionStatusActive, Customer: &stripego.Customer{ID: "cus_1"}, Metadata: map[string]string{"bursar_account_id": "acct_1"}, TrialEnd: 10, CancelAt: 11, CanceledAt: 12, EndedAt: 13, CancelAtPeriodEnd: true, Items: &stripego.SubscriptionItemList{Data: []*stripego.SubscriptionItem{{CurrentPeriodStart: 10, CurrentPeriodEnd: 20, Price: &stripego.Price{ID: "price_1", Product: &stripego.Product{ID: "prod_1"}, Recurring: &stripego.PriceRecurring{Interval: stripego.PriceRecurringIntervalMonth, IntervalCount: 1}}}}}}, "", nil); err != nil || info.Refs == nil || info.Interval != "month" {
		t.Fatalf("subscription projection = %#v, err = %v", info, err)
	}
	if got := stripeUnixPointer(10); got == nil || !got.Equal(time.Unix(10, 0).UTC()) || stripeUnixPointer(0) != nil {
		t.Fatalf("unix pointer = %v", got)
	}
	if got, err := stripeCurrency("usd", "currency"); err != nil || got != "USD" {
		t.Fatalf("currency = %q, err = %v", got, err)
	}
	if _, err := stripeCurrency("US1", "currency"); err == nil {
		t.Fatal("expected invalid currency")
	}
	if got, err := stripeMinorUnits(1, "amount", true); err != nil || got != 1 {
		t.Fatalf("minor amount = %d, err = %v", got, err)
	}
	if _, err := stripeMinorUnits(0, "amount", true); err == nil {
		t.Fatal("expected positive amount validation")
	}
	if got, err := stripeRequiredWebhookText(" value ", "field"); err != nil || got != "value" {
		t.Fatalf("required text = %q, err = %v", got, err)
	}
	if _, err := stripeRequiredWebhookText(" ", "field"); err == nil {
		t.Fatal("expected required text validation")
	}
}

func TestStripeMappingEventsCoverInvoiceRefundAndAutoRecharge(t *testing.T) {
	resources := &stripeWebhookResourcesStub{subscription: &stripego.Subscription{ID: "sub_1", Status: stripego.SubscriptionStatusActive, Customer: &stripego.Customer{ID: "cus_1"}, Metadata: map[string]string{"bursar_account_id": "acct_1"}, Items: &stripego.SubscriptionItemList{Data: []*stripego.SubscriptionItem{{ID: "si_1", CurrentPeriodStart: 10, CurrentPeriodEnd: 20, Price: &stripego.Price{ID: "price_1", Product: &stripego.Product{ID: "prod_1"}}}}}}}
	for _, tc := range []struct {
		name, eventType string
		payload         string
		wantType        bursar.BillingEventType
	}{
		{"auto-recharge succeeded", "payment_intent.succeeded", `{"id":"pi_1","amount":1000,"currency":"usd","customer":"cus_1","metadata":{"bursar_account_id":"acct_1","auto_recharge_attempt_id":"attempt_1","product_id":"prod_1","price_id":"price_1"}}`, bursar.BillingEventPaymentSucceeded},
		{"auto-recharge canceled", "payment_intent.canceled", `{"id":"pi_1","amount":1000,"currency":"usd","customer":"cus_1","metadata":{"bursar_account_id":"acct_1","auto_recharge_attempt_id":"attempt_1","product_id":"prod_1","price_id":"price_1"}}`, bursar.BillingEventPaymentFailed},
		{"refund failed", "refund.failed", `{"id":"re_1","payment_intent":"pi_1","amount":100,"currency":"usd","status":"failed","metadata":{"bursar_account_id":"acct_1"}}`, bursar.BillingEventRefundFailed},
		{"invoice paid", "invoice.paid", `{"id":"in_1","customer":"cus_1","currency":"usd","amount_paid":1000,"amount_due":1000,"hosted_invoice_url":"https://billing.test/in_1","parent":{"subscription_details":{"subscription":"sub_1","metadata":{"bursar_account_id":"acct_1"}}},"period_start":10,"period_end":20}`, bursar.BillingEventInvoicePaid},
		{"invoice failed", "invoice.payment_failed", `{"id":"in_1","customer":"cus_1","currency":"usd","subtotal":1000,"parent":{"subscription_details":{"subscription":"sub_1","metadata":{"bursar_account_id":"acct_1"}}},"total_taxes":[{"amount":100}]}`, bursar.BillingEventPaymentFailed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			event := stripego.Event{ID: "evt_" + tc.name, Created: 1784351724, Type: stripego.EventType(tc.eventType), Data: &stripego.EventData{Raw: json.RawMessage(tc.payload)}}
			mapped, err := mapStripeEvent(context.Background(), event, []byte(tc.payload), resources)
			if err != nil || mapped == nil || mapped.Type != tc.wantType {
				t.Fatalf("mapped = %#v, err = %v", mapped, err)
			}
		})
	}

	chargeEvent := stripego.Event{ID: "evt_charge", Created: 1784351724, Type: "charge.dispute.created", Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"dp_1","status":"needs_response","reason":"fraudulent","payment_intent":"pi_1","metadata":{"bursar_account_id":"acct_1"}}`)}}
	if mapped, err := mapStripeEvent(context.Background(), chargeEvent, []byte(`raw`), resources); err != nil || mapped == nil || mapped.Dispute.ProviderPaymentID != "pi_1" {
		t.Fatalf("dispute = %#v, err = %v", mapped, err)
	}
	if _, err := mapStripeEvent(context.Background(), stripego.Event{ID: "bad", Created: 1, Type: "invoice.paid", Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"in_1"}`)}}, []byte(`raw`), resources); err != nil {
		t.Fatal(err)
	}
}

func TestStripeMappingCheckoutAndRefundVariants(t *testing.T) {
	resources := &stripeWebhookResourcesStub{
		checkout:     &stripego.CheckoutSession{ID: "cs_1", LineItems: &stripego.LineItemList{Data: []*stripego.LineItem{{Price: &stripego.Price{ID: "price_1", Product: &stripego.Product{ID: "prod_1"}}}}}},
		subscription: &stripego.Subscription{ID: "sub_1", Status: stripego.SubscriptionStatusActive, Customer: &stripego.Customer{ID: "cus_1"}, Metadata: map[string]string{"bursar_account_id": "acct_1"}, Items: &stripego.SubscriptionItemList{Data: []*stripego.SubscriptionItem{{ID: "si_1", CurrentPeriodStart: 10, CurrentPeriodEnd: 20, Price: &stripego.Price{ID: "price_1", Product: &stripego.Product{ID: "prod_1"}}}}}},
	}
	checkoutEvents := []struct {
		name, mode, subscription, eventType string
		wantType                            bursar.BillingEventType
	}{
		{"payment", "payment", "", "checkout.session.completed", bursar.BillingEventPaymentSucceeded},
		{"subscription", "subscription", "sub_1", "checkout.session.async_payment_succeeded", bursar.BillingEventCheckoutCompleted},
	}
	for _, tc := range checkoutEvents {
		t.Run(tc.name, func(t *testing.T) {
			payload := `{"id":"cs_1","mode":"` + tc.mode + `","payment_status":"paid","customer":"cus_1","client_reference_id":"acct_1","payment_intent":"pi_1","subscription":"` + tc.subscription + `","amount_subtotal":1000,"amount_total":1100,"currency":"usd","metadata":{"bursar_account_id":"acct_1","plan_slug":"pro"}}`
			event := stripego.Event{ID: "evt_checkout_" + tc.name, Created: 1784351724, Type: stripego.EventType(tc.eventType), Data: &stripego.EventData{Raw: json.RawMessage(payload)}}
			mapped, err := mapStripeEvent(context.Background(), event, []byte(payload), resources)
			if err != nil || mapped == nil || mapped.Type != tc.wantType {
				t.Fatalf("mapped = %#v, err = %v", mapped, err)
			}
		})
	}
	subscriptionPayload := `{"id":"sub_1","status":"active","customer":"cus_1","metadata":{"bursar_account_id":"acct_1"},"items":{"data":[{"id":"si_1","current_period_start":10,"current_period_end":20,"price":{"id":"price_1","product":"prod_1"}}]}}`
	for _, eventType := range []string{"customer.subscription.created", "customer.subscription.paused", "customer.subscription.resumed", "customer.subscription.trial_will_end"} {
		mapped, err := mapStripeEvent(context.Background(), stripego.Event{ID: "evt_" + eventType, Created: 1784351724, Type: stripego.EventType(eventType), Data: &stripego.EventData{Raw: json.RawMessage(subscriptionPayload)}}, []byte(subscriptionPayload), resources)
		if err != nil || mapped == nil {
			t.Fatalf("subscription %s = %#v, err = %v", eventType, mapped, err)
		}
	}

	expiredPayload := `{"id":"cs_expired","mode":"payment","customer":"cus_1","client_reference_id":"acct_1","metadata":{"bursar_account_id":"acct_1"}}`
	expired, err := mapStripeEvent(context.Background(), stripego.Event{ID: "evt_expired", Created: 1784351724, Type: "checkout.session.expired", Data: &stripego.EventData{Raw: json.RawMessage(expiredPayload)}}, []byte(expiredPayload), resources)
	if err != nil || expired == nil || expired.Type != bursar.BillingEventCheckoutExpired {
		t.Fatalf("expired checkout = %#v, err = %v", expired, err)
	}
	for _, tc := range []struct {
		status, eventType string
		wantType          bursar.BillingEventType
	}{
		{"pending", "refund.created", bursar.BillingEventRefundCreated},
		{"succeeded", "refund.updated", bursar.BillingEventRefundUpdated},
		{"canceled", "refund.failed", bursar.BillingEventRefundFailed},
	} {
		raw := `{"id":"re_1","payment_intent":"pi_1","amount":100,"currency":"usd","status":"` + tc.status + `","metadata":{"bursar_account_id":"acct_1"}}`
		mapped, err := mapStripeEvent(context.Background(), stripego.Event{ID: "evt_refund_" + tc.status, Created: 1784351724, Type: stripego.EventType(tc.eventType), Data: &stripego.EventData{Raw: json.RawMessage(raw)}}, []byte(raw), resources)
		if err != nil || mapped == nil || mapped.Type != tc.wantType {
			t.Fatalf("refund %s = %#v, err = %v", tc.status, mapped, err)
		}
	}
}

func TestStripeMappingErrorsAndRequestClassification(t *testing.T) {
	if _, err := stripeInvoiceTaxMinor(nil); err == nil {
		t.Fatal("expected nil invoice error")
	}
	if _, err := stripeInvoiceTaxMinor(&stripego.Invoice{TotalTaxes: []*stripego.InvoiceTotalTax{{Amount: -1}}}); err == nil {
		t.Fatal("expected negative invoice tax error")
	}
	if _, err := stripeInvoiceLineTax(&stripego.InvoiceLineItem{Taxes: []*stripego.InvoiceLineItemTax{{Amount: -1}}}, "line"); err == nil {
		t.Fatal("expected negative line tax error")
	}
	if tax, err := stripeInvoiceLineTax(&stripego.InvoiceLineItem{Taxes: nil}, "line"); err != nil || tax != 0 {
		t.Fatalf("empty line tax = %d, err = %v", tax, err)
	}
	if _, err := stripeInvoiceLineTax(&stripego.InvoiceLineItem{Taxes: []*stripego.InvoiceLineItemTax{nil}}, "line"); err == nil {
		t.Fatal("expected nil line tax error")
	}
	if _, err := stripeInvoiceLineTax(nil, "line"); err == nil {
		t.Fatal("expected nil line error")
	}
	if _, err := stripeInvoiceTotalTax(&stripego.Invoice{TotalTaxes: []*stripego.InvoiceTotalTax{{Amount: -1}}}, "invoice"); err == nil {
		t.Fatal("expected negative total tax error")
	}
	if tax, err := stripeInvoiceTotalTax(&stripego.Invoice{TotalTaxes: nil}, "invoice"); err != nil || tax != 0 {
		t.Fatalf("empty total tax = %d, err = %v", tax, err)
	}
	if _, err := stripeInvoiceTotalTax(&stripego.Invoice{TotalTaxes: []*stripego.InvoiceTotalTax{nil}}, "invoice"); err == nil {
		t.Fatal("expected nil total tax error")
	}
	if _, err := stripeInvoiceTotalTax(nil, "invoice"); err == nil {
		t.Fatal("expected nil invoice total tax error")
	}
	if _, err := stripeSchedulePhaseParams(nil); err == nil {
		t.Fatal("expected nil schedule phase error")
	}
	if _, err := stripeSubscriptionItem(nil, "subscription"); err == nil {
		t.Fatal("expected nil subscription item error")
	}
	for _, subscription := range []*stripego.Subscription{
		{Items: &stripego.SubscriptionItemList{}},
		{Items: &stripego.SubscriptionItemList{Data: []*stripego.SubscriptionItem{nil}}},
		{Items: &stripego.SubscriptionItemList{Data: []*stripego.SubscriptionItem{{}}}},
		{Items: &stripego.SubscriptionItemList{Data: []*stripego.SubscriptionItem{{ID: "one"}, {ID: "two"}}}},
	} {
		if _, err := stripeSubscriptionItem(subscription, "subscription"); err == nil {
			t.Fatal("expected subscription item validation")
		}
	}
	if _, err := stripeCheckoutStatus(nil); err == nil {
		t.Fatal("expected nil checkout status error")
	}
	if err := stripeRequestError("request", &stripego.Error{HTTPStatusCode: 404}, false); err == nil {
		t.Fatal("request error should be non-nil")
	}
	if err := stripeWebhookMappingCause("mapping", errors.New("cause")); err == nil {
		t.Fatal("mapping cause should be non-nil")
	}
}

func TestStripeProviderAndHelperVariants(t *testing.T) {
	fixture := &stripeFixture{}
	provider := newStripeFixtureProvider(t, fixture)
	if provider.Name() != ProviderName || provider.ProviderEnvironment() != bursar.ProviderEnvironmentTest {
		t.Fatal("provider metadata mismatch")
	}
	resources := stripeClientWebhookResources{client: provider.client}
	if checkout, err := resources.retrieveCheckout(t.Context(), "cs_1"); err != nil || checkout == nil {
		t.Fatalf("retrieve checkout = %#v, err = %v", checkout, err)
	}
	if subscription, err := resources.retrieveSubscription(t.Context(), "sub_1"); err != nil || subscription == nil {
		t.Fatalf("retrieve subscription = %#v, err = %v", subscription, err)
	}

	for _, value := range []string{"", "prorated_immediately", "do_not_bill"} {
		if _, err := stripeProrationBehavior(value); err != nil {
			t.Fatalf("proration %q: %v", value, err)
		}
	}
	if _, err := stripeProrationBehavior("invalid"); err == nil {
		t.Fatal("expected invalid proration mode")
	}
	for _, value := range []string{"", "prevent_change", "apply_change"} {
		if _, err := stripePaymentBehavior(value); err != nil {
			t.Fatalf("payment behavior %q: %v", value, err)
		}
	}
	if _, err := stripePaymentBehavior("invalid"); err == nil {
		t.Fatal("expected invalid payment behavior")
	}
	for _, value := range []string{"", "immediately", "next_billing_date"} {
		if _, err := stripeEffectiveAt(value); err != nil {
			t.Fatalf("effective time %q: %v", value, err)
		}
	}
	if _, err := stripeEffectiveAt("invalid"); err == nil {
		t.Fatal("expected invalid effective time")
	}
	if got, err := stripeMultiplyMinor(10, 2, "multiply"); err != nil || got != 20 {
		t.Fatalf("multiplied amount = %d, err = %v", got, err)
	}
	if _, err := stripeMultiplyMinor(-1, 1, "multiply"); err == nil {
		t.Fatal("expected negative amount error")
	}
	if _, err := stripeMultiplyMinor(1, 0, "multiply"); err == nil {
		t.Fatal("expected invalid quantity error")
	}
	if _, err := stripeCardInteger(13, "month", 1, 12); err == nil {
		t.Fatal("expected invalid card month")
	}
	if _, err := requireStripeCardLast4("abcd"); err == nil {
		t.Fatal("expected invalid card last four")
	}
	if _, err := stripeScopedIdempotencyKey(strings.Repeat("x", 256), "scope"); err == nil {
		t.Fatal("expected overlong idempotency key error")
	}

	validPhase := &stripego.SubscriptionSchedulePhase{
		StartDate: 10,
		EndDate:   20,
		Items: []*stripego.SubscriptionSchedulePhaseItem{{
			Price: &stripego.Price{ID: "price_old"}, Quantity: 1,
		}},
	}
	if phase, err := stripeSchedulePhaseParams(validPhase); err != nil || len(phase.Items) != 1 {
		t.Fatalf("schedule phase = %#v, err = %v", phase, err)
	}
	if _, err := stripeSchedulePhaseParams(&stripego.SubscriptionSchedulePhase{StartDate: 10, EndDate: 20, Items: []*stripego.SubscriptionSchedulePhaseItem{{}}}); err == nil {
		t.Fatal("expected schedule price validation")
	}
	optionalPhase := &stripego.SubscriptionSchedulePhase{
		StartDate: 10, EndDate: 20, Description: "renewal", Currency: stripego.CurrencyUSD,
		Items:           []*stripego.SubscriptionSchedulePhaseItem{{Price: &stripego.Price{ID: "price_old"}, Quantity: 1, TaxRates: []*stripego.TaxRate{{ID: "tax_1"}}}},
		DefaultTaxRates: []*stripego.TaxRate{{ID: "tax_2"}}, DefaultPaymentMethod: &stripego.PaymentMethod{ID: "pm_1"},
		AutomaticTax: &stripego.SubscriptionAutomaticTax{Enabled: true}, CollectionMethod: func() *stripego.SubscriptionCollectionMethod {
			value := stripego.SubscriptionCollectionMethodChargeAutomatically
			return &value
		}(),
	}
	if phase, err := stripeSchedulePhaseParams(optionalPhase); err != nil || phase.Description == nil || len(phase.Items[0].TaxRates) != 1 {
		t.Fatalf("optional schedule phase = %#v, err = %v", phase, err)
	}
	for _, invalidPhase := range []*stripego.SubscriptionSchedulePhase{
		{StartDate: 10, EndDate: 20, Items: []*stripego.SubscriptionSchedulePhaseItem{{Price: &stripego.Price{ID: "price"}, TaxRates: []*stripego.TaxRate{nil}}}},
		{StartDate: 10, EndDate: 20, Items: []*stripego.SubscriptionSchedulePhaseItem{{Price: &stripego.Price{ID: "price"}, TaxRates: []*stripego.TaxRate{{}}}}},
		{StartDate: 10, EndDate: 20, Items: []*stripego.SubscriptionSchedulePhaseItem{{Price: &stripego.Price{ID: "price"}}}, DefaultTaxRates: []*stripego.TaxRate{nil}},
		{StartDate: 10, EndDate: 20, Items: []*stripego.SubscriptionSchedulePhaseItem{{Price: &stripego.Price{ID: "price"}}}, DefaultTaxRates: []*stripego.TaxRate{{}}},
		{StartDate: 10, EndDate: 20, Items: []*stripego.SubscriptionSchedulePhaseItem{{Price: &stripego.Price{ID: "price"}}}, DefaultPaymentMethod: &stripego.PaymentMethod{}},
	} {
		if _, err := stripeSchedulePhaseParams(invalidPhase); err == nil {
			t.Fatal("expected invalid optional schedule phase")
		}
	}
	for _, status := range []string{"expired", "paid", "no_payment_required", "open", "complete"} {
		session := &stripego.CheckoutSession{Status: stripego.CheckoutSessionStatus(status)}
		if status == "paid" || status == "no_payment_required" {
			session.PaymentStatus = stripego.CheckoutSessionPaymentStatus(status)
		}
		if _, err := stripeCheckoutStatus(session); err != nil {
			t.Fatal(err)
		}
	}
	for _, status := range []stripego.PaymentIntentStatus{
		stripego.PaymentIntentStatusSucceeded, stripego.PaymentIntentStatusProcessing, stripego.PaymentIntentStatusRequiresAction,
		stripego.PaymentIntentStatusRequiresPaymentMethod, stripego.PaymentIntentStatusRequiresConfirmation, stripego.PaymentIntentStatusRequiresCapture,
		stripego.PaymentIntentStatusCanceled,
	} {
		session := &stripego.CheckoutSession{PaymentIntent: &stripego.PaymentIntent{Status: status}}
		if _, err := stripeCheckoutStatus(session); err != nil {
			t.Fatal(err)
		}
	}

	for _, status := range []int{404, 409, 429, 500} {
		err := stripeRequestError("request", &stripego.Error{HTTPStatusCode: status}, true)
		if err == nil {
			t.Fatal("expected classified request error")
		}
	}
	if stripeRequestError("request", context.Canceled, true) == nil {
		t.Fatal("expected canceled request error")
	}
	for _, stripeErr := range []*stripego.Error{
		{Type: stripego.ErrorTypeCard, HTTPStatusCode: 402},
		{HTTPStatusCode: 400},
		{HTTPStatusCode: 500},
	} {
		if stripeRequestError("request", stripeErr, true) == nil {
			t.Fatal("expected provider request classification")
		}
	}
	validRequest := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, IdempotencyKey: "key"}
	for _, invalid := range []bursar.ProviderPlanChangeRequest{
		{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 0},
		{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, EffectiveAt: "bad"},
		{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, ProrationBillingMode: "bad"},
		{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, IdempotencyKey: strings.Repeat("x", 256)},
		{ProviderSubscriptionID: "sub", ProductID: "price", Quantity: 1, IdempotencyKey: "key", PaymentFailure: "bad"},
	} {
		if _, err := normalizeStripePlanChangeRequest(invalid, true); err == nil {
			t.Fatal("expected plan change normalization error")
		}
	}
	if normalized, err := normalizeStripePlanChangeRequest(validRequest, true); err != nil || normalized.EffectiveAt != "immediately" {
		t.Fatalf("normalized plan request = %#v, err = %v", normalized, err)
	}
}

func TestStripeRemainingValidationAndResourceBranches(t *testing.T) {
	for _, request := range []bursar.CheckoutSessionRequest{
		{AccountID: "acct", ProductID: "price", Quantity: 1, SuccessURL: "", CancelURL: "cancel", IdempotencyKey: "key"},
		{AccountID: "acct", ProductID: "price", Quantity: 1, SuccessURL: "success", CancelURL: "", IdempotencyKey: "key"},
		{AccountID: "acct", ProductID: "price", Quantity: 1, SuccessURL: "success", CancelURL: "cancel"},
		{AccountID: "acct", ProductID: "price", Quantity: 0, SuccessURL: "success", CancelURL: "cancel", IdempotencyKey: "key"},
	} {
		if err := validateCheckoutRequest(request); err == nil {
			t.Fatal("expected checkout validation error")
		}
	}
	if err := stripeWebhookResourceError("resource", errors.New("down")); err == nil {
		t.Fatal("resource error should be non-nil")
	}
	if err := stripeDecodeEventObject(stripego.Event{Data: &stripego.EventData{Raw: []byte("{")}}, &stripego.CheckoutSession{}); err == nil {
		t.Fatal("expected malformed event object error")
	}
	if got := stripeCustomerInfo("", "", "acct", nil); got != nil {
		t.Fatal("empty customer should be nil")
	}
	if customer := stripeCustomerInfo("cus_1", "buyer@example.com", "acct_1", map[string]string{"key": "value"}); customer == nil || customer.Email != "buyer@example.com" {
		t.Fatalf("customer projection = %#v", customer)
	}
	if got := stripeCheckoutCustomer(nil, "acct", nil); got != nil {
		t.Fatal("nil checkout customer should be nil")
	}
	if err := validateStripeSavedPaymentChargeParams(bursar.SavedPaymentChargeParams{PaymentMethodID: "pm", ProductID: "price", Quantity: 1, IdempotencyKey: "key"}); err == nil {
		t.Fatal("expected saved payment customer validation")
	}
	if err := validateStripeSavedPaymentChargeParams(bursar.SavedPaymentChargeParams{CustomerID: "cus", ProductID: "price", Quantity: 1, IdempotencyKey: "key"}); err == nil {
		t.Fatal("expected saved payment method validation")
	}
	if err := validateStripeSavedPaymentChargeParams(bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", Quantity: 1, IdempotencyKey: "key"}); err == nil {
		t.Fatal("expected saved payment price validation")
	}
	if err := validateStripeSavedPaymentChargeParams(bursar.SavedPaymentChargeParams{CustomerID: "cus", PaymentMethodID: "pm", ProductID: "price", Quantity: 1}); err == nil {
		t.Fatal("expected saved payment key validation")
	}
	for _, response := range []string{
		`{"id":"price_1","active":false,"billing_scheme":"per_unit","currency":"usd","unit_amount":1000}`,
		`{"id":"price_1","active":true,"deleted":true,"billing_scheme":"per_unit","currency":"usd","unit_amount":1000}`,
		`{"id":"price_1","active":true,"billing_scheme":"tiered","currency":"usd","unit_amount":1000}`,
		`{"id":"price_1","active":true,"billing_scheme":"per_unit","custom_unit_amount":{"enabled":true},"currency":"usd","unit_amount":1000}`,
		`{"id":"price_1","active":true,"billing_scheme":"per_unit","currency":"US1","unit_amount":1000}`,
		`{"id":"price_1","active":true,"billing_scheme":"per_unit","currency":"usd","unit_amount":-1}`,
	} {
		if _, err := retrieveFixedPriceForTest(t, response); err == nil {
			t.Fatal("expected invalid price validation")
		}
	}
	if _, err := stripeInvoiceTotalTax(&stripego.Invoice{TotalTaxes: []*stripego.InvoiceTotalTax{{Amount: 30}}}, "invoice"); err != nil {
		t.Fatal(err)
	}
	if _, err := stripeInvoiceLineTax(&stripego.InvoiceLineItem{Taxes: []*stripego.InvoiceLineItemTax{{Amount: 30}}}, "line"); err != nil {
		t.Fatal(err)
	}
	if customer := stripeCheckoutCustomer(&stripego.CheckoutSession{Customer: &stripego.Customer{ID: "cus_1", Deleted: true}, CustomerDetails: &stripego.CheckoutSessionCustomerDetails{Email: "buyer@example.com"}}, "acct_1", nil); customer == nil || customer.Email != "buyer@example.com" {
		t.Fatalf("checkout customer details = %#v", customer)
	}
	validSession := &stripego.CheckoutSession{ID: "cs_1", PaymentIntent: &stripego.PaymentIntent{ID: "pi_1"}, Currency: stripego.CurrencyUSD, AmountTotal: 1200, TotalDetails: &stripego.CheckoutSessionTotalDetails{AmountTax: 200}}
	validExpanded := &stripego.CheckoutSession{LineItems: &stripego.LineItemList{Data: []*stripego.LineItem{{Price: &stripego.Price{ID: "price_1", Product: &stripego.Product{ID: "prod_1"}}}}}}
	if payment, err := stripeCheckoutPayment(validSession, validExpanded, "succeeded", "acct_1", nil); err != nil || payment.AmountMinor != 1000 || payment.TaxMinor != 200 || payment.Refs == nil {
		t.Fatalf("checkout payment = %#v, err = %v", payment, err)
	}
	if got, err := stripeCheckoutStatus(&stripego.CheckoutSession{Status: "unknown"}); err != nil || got != "" {
		t.Fatalf("unknown checkout status = %q, err = %v", got, err)
	}
	for _, invalid := range []*stripego.CheckoutSession{nil, &stripego.CheckoutSession{ID: "cs_1"}, &stripego.CheckoutSession{ID: "cs_1", PaymentIntent: &stripego.PaymentIntent{ID: "pi_1"}, Currency: "US1"}} {
		if _, err := stripeCheckoutPayment(invalid, validExpanded, "succeeded", "acct", nil); err == nil {
			t.Fatal("expected checkout payment validation error")
		}
	}
	if _, err := stripeCheckoutPayment(validSession, nil, "succeeded", "acct", nil); err == nil {
		t.Fatal("expected missing expanded checkout response error")
	}
}

func retrieveFixedPriceForTest(t *testing.T, response string) (*stripego.Price, error) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(response))
	}))
	t.Cleanup(server.Close)
	client := stripego.NewClient("sk_test", stripego.WithBackends(stripego.NewBackendsWithConfig(&stripego.BackendConfig{HTTPClient: server.Client(), URL: stripego.String(server.URL), MaxNetworkRetries: stripego.Int64(0)})))
	provider := &Provider{client: client}
	return provider.retrieveFixedPrice(t.Context(), "price_1", "test", true)
}

func TestStripePlanPreviewAndScheduleValidationMatrix(t *testing.T) {
	if _, err := (&Provider{}).stripePlanChangePreview(t.Context(), nil, nil, nil, "immediately"); err == nil {
		t.Fatal("expected nil plan preview response error")
	}
	validInvoice := &stripego.Invoice{
		Total: 1200, AmountDue: 1200, Currency: stripego.CurrencyUSD, Created: 10,
		Lines: &stripego.InvoiceLineItemList{Data: []*stripego.InvoiceLineItem{{
			Description: "Pro", Currency: stripego.CurrencyUSD, Quantity: 1, Subtotal: 1000,
			Parent:  &stripego.InvoiceLineItemParent{SubscriptionItemDetails: &stripego.InvoiceLineItemParentSubscriptionItemDetails{}},
			Pricing: &stripego.InvoiceLineItemPricing{PriceDetails: &stripego.InvoiceLineItemPricingPriceDetails{Price: "price_1"}},
		}}},
	}
	validPrice := &stripego.Price{ID: "price_1", UnitAmount: 1000, Currency: stripego.CurrencyUSD}
	validItem := &stripego.SubscriptionItem{ID: "si_1", CurrentPeriodEnd: 20}
	provider := newStripeFixtureProvider(t, &stripeFixture{})
	if preview, err := provider.stripePlanChangePreview(t.Context(), validInvoice, validPrice, validItem, "immediately"); err != nil || preview.TotalAmount.IntPart() != 1200 {
		t.Fatalf("valid plan preview = %#v, err = %v", preview, err)
	}
	if preview, err := provider.stripePlanChangePreview(t.Context(), validInvoice, validPrice, validItem, "next_billing_date"); err != nil || preview.EffectiveAt.Unix() != 20 {
		t.Fatalf("next-billing preview = %#v, err = %v", preview, err)
	}
	zeroQuantityInvoice := *validInvoice
	zeroQuantityLines := *validInvoice.Lines
	zeroQuantityLine := *zeroQuantityLines.Data[0]
	zeroQuantityLine.Quantity = 0
	zeroQuantityLines.Data = []*stripego.InvoiceLineItem{&zeroQuantityLine}
	zeroQuantityInvoice.Lines = &zeroQuantityLines
	if preview, err := provider.stripePlanChangePreview(t.Context(), &zeroQuantityInvoice, validPrice, validItem, "immediately"); err != nil || len(preview.LineItems) != 1 || preview.LineItems[0].Quantity != 1 {
		t.Fatalf("zero-quantity preview = %#v, err = %v", preview, err)
	}
	for _, tc := range []struct {
		name    string
		invoice *stripego.Invoice
		price   *stripego.Price
		item    *stripego.SubscriptionItem
	}{
		{"nil invoice", nil, validPrice, validItem},
		{"negative total", &stripego.Invoice{Total: -1, AmountDue: 1, Currency: stripego.CurrencyUSD, Lines: validInvoice.Lines}, validPrice, validItem},
		{"negative due", &stripego.Invoice{Total: 1, AmountDue: -1, Currency: stripego.CurrencyUSD, Lines: validInvoice.Lines}, validPrice, validItem},
		{"nil price", validInvoice, nil, validItem},
		{"nil item", validInvoice, validPrice, nil},
		{"missing period", validInvoice, validPrice, &stripego.SubscriptionItem{ID: "si_1"}},
		{"bad currency", &stripego.Invoice{Total: 1, AmountDue: 1, Currency: "US1", Lines: validInvoice.Lines}, validPrice, validItem},
		{"nil lines", &stripego.Invoice{Total: 1, AmountDue: 1, Currency: stripego.CurrencyUSD}, validPrice, validItem},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := provider.stripePlanChangePreview(t.Context(), tc.invoice, tc.price, tc.item, "immediately"); err == nil {
				t.Fatal("expected preview validation error")
			}
		})
	}
	for _, line := range []*stripego.InvoiceLineItem{
		{Parent: &stripego.InvoiceLineItemParent{SubscriptionItemDetails: &stripego.InvoiceLineItemParentSubscriptionItemDetails{}}, Pricing: &stripego.InvoiceLineItemPricing{PriceDetails: &stripego.InvoiceLineItemPricingPriceDetails{}}, Description: "Pro", Currency: stripego.CurrencyUSD, Quantity: 1},
		{Parent: &stripego.InvoiceLineItemParent{SubscriptionItemDetails: &stripego.InvoiceLineItemParentSubscriptionItemDetails{}}, Pricing: &stripego.InvoiceLineItemPricing{PriceDetails: &stripego.InvoiceLineItemPricingPriceDetails{Price: "price_1"}}, Description: "", Currency: stripego.CurrencyUSD, Quantity: 1},
		{Parent: &stripego.InvoiceLineItemParent{SubscriptionItemDetails: &stripego.InvoiceLineItemParentSubscriptionItemDetails{}}, Pricing: &stripego.InvoiceLineItemPricing{PriceDetails: &stripego.InvoiceLineItemPricingPriceDetails{Price: "price_1"}}, Description: "Pro", Currency: "US1", Quantity: 1},
		{Parent: &stripego.InvoiceLineItemParent{SubscriptionItemDetails: &stripego.InvoiceLineItemParentSubscriptionItemDetails{}}, Pricing: &stripego.InvoiceLineItemPricing{PriceDetails: &stripego.InvoiceLineItemPricingPriceDetails{Price: "price_1"}}, Description: "Pro", Currency: stripego.CurrencyUSD, Quantity: -1},
		{Parent: &stripego.InvoiceLineItemParent{SubscriptionItemDetails: &stripego.InvoiceLineItemParentSubscriptionItemDetails{}}, Pricing: nil, Description: "Pro", Currency: stripego.CurrencyUSD, Quantity: 1},
	} {
		invoice := *validInvoice
		lines := *validInvoice.Lines
		lines.Data = []*stripego.InvoiceLineItem{line}
		invoice.Lines = &lines
		if _, err := provider.stripePlanChangePreview(t.Context(), &invoice, validPrice, validItem, "immediately"); err == nil {
			t.Fatal("expected line validation error")
		}
	}
	for _, invalidInvoice := range []*stripego.Invoice{
		{Total: 1, AmountDue: 1, Currency: stripego.CurrencyUSD, Created: 0, Lines: validInvoice.Lines},
		{Total: 1, AmountDue: 1, Currency: stripego.CurrencyUSD, Created: 10, TotalTaxes: []*stripego.InvoiceTotalTax{{Amount: -1}}, Lines: validInvoice.Lines},
	} {
		if _, err := provider.stripePlanChangePreview(t.Context(), invalidInvoice, validPrice, validItem, "immediately"); err == nil {
			t.Fatal("expected invalid invoice preview")
		}
	}
}

type failingStripeResources struct{}

func (failingStripeResources) retrieveCheckout(context.Context, string) (*stripego.CheckoutSession, error) {
	return nil, errors.New("checkout unavailable")
}

func (failingStripeResources) retrieveSubscription(context.Context, string) (*stripego.Subscription, error) {
	return nil, errors.New("subscription unavailable")
}

func TestStripeEventMappingFailureMatrix(t *testing.T) {
	resources := &stripeWebhookResourcesStub{}
	for _, event := range []stripego.Event{
		{Created: 1, Type: "product.updated", Data: &stripego.EventData{Raw: []byte(`{}`)}},
		{ID: "negative", Created: -1, Type: "product.updated", Data: &stripego.EventData{Raw: []byte(`{}`)}},
		{ID: "empty-data", Created: 1, Type: "customer.subscription.created"},
	} {
		if _, err := mapStripeEvent(context.Background(), event, []byte(`raw`), resources); err == nil {
			t.Fatal("expected event validation error")
		}
	}
	unknown, err := mapStripeEvent(context.Background(), stripego.Event{ID: "unknown", Created: 1, Type: "product.updated", Data: &stripego.EventData{Raw: []byte(`{}`)}}, []byte(`raw`), resources)
	if err != nil || unknown != nil {
		t.Fatalf("unknown event = %#v, err = %v", unknown, err)
	}
	unpaid := stripego.Event{ID: "unpaid", Created: 1, Type: "checkout.session.completed", Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"cs_1","payment_status":"unpaid"}`)}}
	if mapped, err := mapStripeEvent(context.Background(), unpaid, []byte(`raw`), resources); err != nil || mapped != nil {
		t.Fatalf("unpaid checkout = %#v, err = %v", mapped, err)
	}
	checkout := stripego.Event{ID: "checkout", Created: 1, Type: "checkout.session.completed", Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"cs_1","payment_status":"paid"}`)}}
	if _, err := mapStripeEvent(context.Background(), checkout, []byte(`raw`), nil); err == nil {
		t.Fatal("expected missing checkout resources error")
	}
	if _, err := mapStripeEvent(context.Background(), checkout, []byte(`raw`), failingStripeResources{}); err == nil {
		t.Fatal("expected checkout resource error")
	}
	badPayment := stripego.Event{ID: "payment", Created: 1, Type: "payment_intent.succeeded", Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"pi_1","amount":1,"currency":"usd","metadata":{"auto_recharge_attempt_id":"attempt"}}`)}}
	if _, err := mapStripeEvent(context.Background(), badPayment, []byte(`raw`), resources); err == nil {
		t.Fatal("expected missing payment account validation")
	}
	badRefund := stripego.Event{ID: "refund", Created: 1, Type: "refund.created", Data: &stripego.EventData{Raw: json.RawMessage(`{"id":"re_1","payment_intent":"pi_1","amount":0,"currency":"usd"}`)}}
	if _, err := mapStripeEvent(context.Background(), badRefund, []byte(`raw`), resources); err == nil {
		t.Fatal("expected invalid refund amount")
	}
	if _, err := stripeFinalizeEvent(stripego.Event{ID: "", Created: 1}, []byte(`raw`), bursar.BillingEventPaymentSucceeded, "", nil, nil, nil, nil, nil, nil, nil, time.Unix(1, 0)); err == nil {
		t.Fatal("expected invalid normalized event")
	}
}
