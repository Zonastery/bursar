// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package dodo

import (
	"encoding/json"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

func TestDodoEventHelperVariants(t *testing.T) {
	occurred := map[string]any{"payment_id": "pay_1", "subscription_id": "sub_1", "product_id": "prod_1", "payment_frequency_interval": "month", "payment_frequency_count": json.Number("2"), "previous_billing_date": "2026-07-18T05:15:24Z", "next_billing_date": "2026-08-18T05:15:24Z", "cancel_at_next_billing_date": true}
	if id, err := dodoEventResourceID("payment.succeeded", occurred); err != nil || id != "pay_1" {
		t.Fatalf("payment resource = %q, %v", id, err)
	}
	if id, err := dodoEventResourceID("subscription.updated", occurred); err != nil || id != "sub_1" {
		t.Fatalf("subscription resource = %q, %v", id, err)
	}
	if id, err := dodoEventResourceID("refund.succeeded", map[string]any{"id": "ref_1"}); err != nil || id != "ref_1" {
		t.Fatalf("refund fallback = %q, %v", id, err)
	}
	if id, err := dodoEventResourceID("dispute.opened", map[string]any{"id": "dis_1"}); err != nil || id != "dis_1" {
		t.Fatalf("dispute fallback = %q, %v", id, err)
	}
	if _, err := dodoEventResourceID("payment.succeeded", map[string]any{}); err == nil {
		t.Fatal("expected missing event resource")
	}
	if product, err := dodoProductID(map[string]any{"product_cart": []any{map[string]any{}, map[string]any{"product_id": "prod_cart"}}}); err != nil || product != "prod_cart" {
		t.Fatalf("cart product = %q, %v", product, err)
	}
	for _, value := range []any{"bad", []any{"bad"}} {
		if _, err := dodoProductID(map[string]any{"product_cart": value}); err == nil {
			t.Fatal("expected invalid product cart")
		}
	}
	if refs, err := dodoSubscriptionRefs(map[string]any{}, map[string]string{"plan_slug": "pro"}); err != nil || refs == nil || refs.LookupKey != "pro" {
		t.Fatalf("metadata refs = %#v, %v", refs, err)
	}
	if refs, err := dodoSubscriptionRefs(map[string]any{"product_id": "prod"}, nil); err != nil || refs == nil || refs.ProductID != "prod" {
		t.Fatalf("product refs = %#v, %v", refs, err)
	}
	if _, err := dodoSubscriptionRefs(map[string]any{"product_id": 1}, nil); err == nil {
		t.Fatal("expected invalid product ref")
	}
	if sub, err := dodoSubscription(occurred, map[string]string{}, "acct", "sub_1", "active"); err != nil || sub.Interval != "month" || sub.IntervalCount != 2 || !sub.CancelAtPeriodEnd {
		t.Fatalf("subscription = %#v, %v", sub, err)
	}
	if _, err := dodoSubscription(map[string]any{"payment_frequency_count": json.Number("2")}, nil, "acct", "sub", "active"); err == nil {
		t.Fatal("expected interval count without interval error")
	}
	for _, status := range []struct{ input, want string }{{"pending", "incomplete"}, {"trialing", "trialing"}, {"active", "active"}, {"paused", "paused"}, {"expired", "expired"}, {"on_hold", "past_due"}, {"failed", "past_due"}, {"cancelled", "canceled"}, {"future", ""}} {
		if got, err := dodoSubscriptionStatus(status.input); err != nil || got != status.want {
			t.Fatalf("status %q = %q, %v", status.input, got, err)
		}
	}
	if _, err := dodoSubscriptionStatus(1); err == nil {
		t.Fatal("expected invalid subscription status")
	}
	customer, err := dodoCustomer(map[string]any{"customer": map[string]any{"customer_id": "cus", "email": "buyer@example.com"}}, "acct", map[string]string{"key": "value"})
	if err != nil || customer == nil || customer.ProviderCustomerID != "cus" {
		t.Fatalf("customer = %#v, %v", customer, err)
	}
	if customer, err := dodoCustomer(map[string]any{}, "acct", nil); err != nil || customer != nil {
		t.Fatalf("empty customer = %#v, %v", customer, err)
	}
	if _, err := dodoCustomer(map[string]any{"customer": "bad"}, "acct", nil); err == nil {
		t.Fatal("expected invalid customer object")
	}
}

func TestDodoScalarMapperVariants(t *testing.T) {
	if metadata, err := dodoWebhookMetadata(nil); err != nil || len(metadata) != 0 {
		t.Fatalf("nil metadata = %#v, %v", metadata, err)
	}
	for _, value := range []any{"value", true, json.Number("12"), float64(1.5)} {
		if _, err := dodoWebhookMetadata(map[string]any{"value": value}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := dodoWebhookMetadata(map[string]any{"object": map[string]any{}}); err == nil {
		t.Fatal("expected object metadata error")
	}
	if _, err := dodoWebhookMetadata(map[string]any{"number": json.Number("bad")}); err == nil {
		t.Fatal("expected invalid number metadata")
	}
	if _, err := dodoOptionalObject("bad", "object"); err == nil {
		t.Fatal("expected object type error")
	}
	if _, err := dodoOptionalBool("bad", "bool"); err == nil {
		t.Fatal("expected bool type error")
	}
	if got, err := dodoOptionalBool(true, "bool"); err != nil || got == nil || !*got {
		t.Fatalf("bool = %v, %v", got, err)
	}
	if got, err := dodoFirstRequiredText("field", nil, " value "); err != nil || got != "value" {
		t.Fatalf("first text = %q, %v", got, err)
	}
	for _, value := range []any{nil, "", 1} {
		if _, err := dodoRequiredText(value, "field"); err == nil {
			t.Fatal("expected required text error")
		}
	}
	if _, err := dodoFirstRequiredText("field", nil); err == nil {
		t.Fatal("expected first text error")
	}
	for _, value := range []any{json.Number("1"), int64(2), int(3), "4"} {
		if got, err := dodoMinorUnits(value, "amount", false); err != nil || got < 1 {
			t.Fatalf("minor units %v = %d, %v", value, got, err)
		}
	}
	for _, value := range []any{"", "-1", float64(1), json.Number("0")} {
		if _, err := dodoMinorUnits(value, "amount", true); err == nil {
			t.Fatal("expected invalid minor units")
		}
	}
	if got, err := dodoCurrency("usd", "currency"); err != nil || got != "USD" {
		t.Fatalf("currency = %q, %v", got, err)
	}
	for _, value := range []any{"US", "US1", 1} {
		if _, err := dodoCurrency(value, "currency"); err == nil {
			t.Fatal("expected invalid currency")
		}
	}
	if got, err := dodoOptionalTime(nil, "time"); err != nil || got != nil {
		t.Fatal(err)
	}
	for _, value := range []any{"2026-07-18T05:15:24Z", "Sat Jul 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)", "bad", 1} {
		if _, err := dodoOptionalTime(value, "time"); value == "bad" || value == 1 {
			if err == nil {
				t.Fatal("expected invalid optional time")
			}
		} else if err != nil {
			t.Fatalf("time %v: %v", value, err)
		}
	}
	if err := dodoWebhookMappingCause("mapping", bursar.NewError("cause", bursar.ErrorOptions{})); err == nil {
		t.Fatal("expected mapping cause")
	}
}

func TestDodoPaymentRefundAndDisputeHelpers(t *testing.T) {
	base := map[string]any{"payment_id": "pay_1", "total_amount": json.Number("100"), "currency": "usd", "product_id": "prod_1"}
	if id, payment, subscription, err := dodoPayment(base, map[string]string{}, "acct", false, true); err != nil || id != "pay_1" || payment.Status != "canceled" || subscription != nil {
		t.Fatalf("canceled payment = %#v, %#v, %v", payment, subscription, err)
	}
	succeeded := map[string]any{"payment_id": "pay_1", "subscription_id": "sub_1", "settlement_amount": json.Number("100"), "settlement_currency": "usd", "subscription_status": "on_hold", "previous_billing_date": "2026-07-18T05:15:24Z", "next_billing_date": "2026-08-18T05:15:24Z"}
	if _, payment, subscription, err := dodoPayment(succeeded, map[string]string{}, "acct", true, false); err != nil || payment.Status != "succeeded" || subscription == nil || subscription.Status != "past_due" {
		t.Fatalf("succeeded payment = %#v, %#v, %v", payment, subscription, err)
	}
	if _, _, _, err := dodoPayment(map[string]any{"payment_id": "pay", "total_amount": 1, "currency": "USD", "subscription_id": 1}, nil, "acct", false, false); err == nil {
		t.Fatal("expected invalid payment subscription")
	}
	refund := map[string]any{"id": "ref_1", "payment_id": "pay_1", "amount": json.Number("10"), "currency": "USD", "reason": "requested"}
	if id, value, err := dodoRefund(refund, nil, true); err != nil || id != "ref_1" || value.Status != "succeeded" || value.Reason != "requested" {
		t.Fatalf("refund = %#v, %v", value, err)
	}
	if _, _, err := dodoRefund(map[string]any{"refund_id": "ref", "payment_id": "pay", "refund_amount": 0, "currency": "USD"}, nil, false); err == nil {
		t.Fatal("expected invalid refund amount")
	}
	if id, value, err := dodoDispute(map[string]any{"id": "dis_1", "payment_id": "pay_1"}, nil, "dispute.opened"); err != nil || id != "dis_1" || value.Status != "needs_response" {
		t.Fatalf("dispute = %#v, %v", value, err)
	}
	if _, _, err := dodoDispute(map[string]any{"dispute_id": "dis", "payment_id": "pay", "reason": 1}, nil, "dispute.won"); err == nil {
		t.Fatal("expected invalid dispute reason")
	}
}

func TestDodoSubscriptionEventVariants(t *testing.T) {
	for _, providerType := range []string{"subscription.active", "subscription.renewed", "subscription.paused", "subscription.updated", "subscription.plan_changed"} {
		data := map[string]any{"subscription_id": "sub_1", "product_id": "prod_1", "status": "active"}
		if providerType == "subscription.paused" {
			data["status"] = "paused"
		}
		if event, err := mapDodoEvent(providerType, testDodoOccurredAt, nil, data); err != nil || event == nil {
			t.Fatalf("%s = %#v, %v", providerType, event, err)
		}
	}
	if _, err := mapDodoEvent("subscription.active", testDodoOccurredAt, nil, map[string]any{"subscription_id": "sub", "status": 1}); err == nil {
		t.Fatal("expected invalid active status")
	}
}

func TestDodoSubscriptionIntervalFallbacks(t *testing.T) {
	if interval, err := dodoSubscriptionInterval(nil, map[string]string{"billing_interval": "year"}); err != nil || interval != "year" {
		t.Fatalf("metadata interval = %q, %v", interval, err)
	}
	if _, err := dodoSubscriptionInterval(nil, map[string]string{"billing_interval": "hour"}); err == nil {
		t.Fatal("expected invalid metadata interval")
	}
	if count, err := dodoSubscriptionIntervalCount(nil, "month"); err != nil || count != 1 {
		t.Fatalf("default interval count = %d, %v", count, err)
	}
	if count, err := dodoSubscriptionIntervalCount(nil, ""); err != nil || count != 0 {
		t.Fatalf("empty interval count = %d, %v", count, err)
	}
}

var testDodoOccurredAt = func() time.Time {
	return time.Date(2026, 7, 18, 5, 15, 24, 0, time.UTC)
}()
