// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package dodo

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	dodopayments "github.com/dodopayments/dodopayments-go"
)

func TestDodoValidationAndEnumHelpers(t *testing.T) {
	for _, value := range []string{"", "prorated_immediately", "full_immediately", "difference_immediately", "do_not_bill"} {
		if _, err := dodoProrationMode(value); err != nil {
			t.Fatalf("proration %q: %v", value, err)
		}
	}
	if _, err := dodoProrationMode("invalid"); err == nil {
		t.Fatal("expected invalid proration")
	}
	for _, value := range []string{"", "immediately", "next_billing_date"} {
		if _, err := dodoEffectiveAt(value); err != nil {
			t.Fatalf("effective %q: %v", value, err)
		}
	}
	if _, err := dodoEffectiveAt("invalid"); err == nil {
		t.Fatal("expected invalid effective time")
	}
	for _, value := range []string{"prevent_change", "apply_change"} {
		if _, err := dodoPaymentFailureMode(value); err != nil {
			t.Fatalf("payment failure %q: %v", value, err)
		}
	}
	if _, err := dodoPaymentFailureMode("invalid"); err == nil {
		t.Fatal("expected invalid payment failure")
	}
	for _, status := range []dodopayments.IntentStatus{
		dodopayments.IntentStatusSucceeded, dodopayments.IntentStatusFailed, dodopayments.IntentStatusCancelled,
		dodopayments.IntentStatusProcessing, dodopayments.IntentStatusRequiresCustomerAction, dodopayments.IntentStatusRequiresMerchantAction,
		dodopayments.IntentStatusRequiresPaymentMethod, dodopayments.IntentStatusRequiresConfirmation, dodopayments.IntentStatusRequiresCapture,
		dodopayments.IntentStatusPartiallyCaptured, dodopayments.IntentStatusPartiallyCapturedAndCapturable,
	} {
		if got, err := dodoSavedPaymentStatus(status); err != nil || string(got) != string(status) {
			t.Fatalf("status %q = %q, %v", status, got, err)
		}
	}
	if _, err := dodoSavedPaymentStatus("unknown"); err == nil {
		t.Fatal("expected invalid saved payment status")
	}
	if got, err := requireDodoCardLast4(" 4242 "); err != nil || got != "4242" {
		t.Fatalf("last4 = %q, %v", got, err)
	}
	for _, value := range []string{"", "123", "12a4", "12345"} {
		if _, err := requireDodoCardLast4(value); err == nil {
			t.Fatalf("expected invalid card last4 %q", value)
		}
	}
	if got, err := parseDodoCardInteger(" 12 ", "month", 1, 12); err != nil || got != 12 {
		t.Fatalf("card integer = %d, %v", got, err)
	}
	for _, value := range []string{"", "0", "13", "x"} {
		if _, err := parseDodoCardInteger(value, "month", 1, 12); err == nil {
			t.Fatalf("expected invalid card integer %q", value)
		}
	}
	if got, err := dodoProviderFloatAmount(1.25, "amount", "value"); err != nil || got.String() != "1.25" {
		t.Fatalf("float amount = %s, %v", got, err)
	}
	for _, value := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if _, err := dodoProviderFloatAmount(value, "amount", "value"); err == nil {
			t.Fatal("expected non-finite float error")
		}
	}
	if firstNonEmpty(" ", "value", "other") != "value" || firstNonEmpty("", " ") != "" {
		t.Fatal("unexpected first non-empty value")
	}
}

func TestDodoRequestAndPreviewHelpers(t *testing.T) {
	for _, request := range []bursar.CheckoutSessionRequest{
		{}, {AccountID: "acct", ProductID: "prod", Quantity: 0, SuccessURL: "ok", CancelURL: "cancel", IdempotencyKey: "key"},
		{AccountID: "acct", ProductID: "prod", Quantity: 1, SuccessURL: "", CancelURL: "cancel", IdempotencyKey: "key"},
		{AccountID: "acct", ProductID: "prod", Quantity: 1, SuccessURL: "ok", CancelURL: "", IdempotencyKey: "key"},
		{AccountID: "acct", ProductID: "prod", Quantity: 1, SuccessURL: "ok", CancelURL: "cancel"},
	} {
		if err := validateCheckoutRequest(request); err == nil {
			t.Fatal("expected checkout validation error")
		}
	}
	valid := bursar.SavedPaymentChargeParams{CustomerID: "cus", ProductID: "prod", Quantity: 1}
	if err := validateSavedPaymentChargeParams(valid, false); err != nil {
		t.Fatal(err)
	}
	for _, request := range []bursar.SavedPaymentChargeParams{
		{}, {CustomerID: "cus", Quantity: 1}, {CustomerID: "cus", ProductID: "prod", Quantity: 0},
		{CustomerID: "cus", ProductID: "prod", Quantity: 1, PaymentMethodID: "pm"},
		{CustomerID: "cus", ProductID: "prod", Quantity: 1, PaymentMethodID: "pm", ReturnURL: "return"},
	} {
		if err := validateSavedPaymentChargeParams(request, true); err == nil {
			t.Fatal("expected saved payment validation error")
		}
	}
	request := bursar.ProviderPlanChangeRequest{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 2, EffectiveAt: "next_billing_date", ProrationBillingMode: "full_immediately", PaymentFailure: "apply_change", IdempotencyKey: "key", Metadata: map[string]string{"a": "b"}}
	if id, body, err := dodoPlanChangeRequest(request, true); err != nil || id != "sub" || !body.Quantity.Present || !body.OnPaymentFailure.Present || !body.Metadata.Present {
		t.Fatalf("plan body = %#v, err = %v", body, err)
	}
	if _, _, err := dodoPlanChangeRequest(request, false); err != nil {
		t.Fatal(err)
	}
	for _, invalid := range []bursar.ProviderPlanChangeRequest{
		{}, {ProviderSubscriptionID: "sub"}, {ProviderSubscriptionID: "sub", ProductID: "prod"},
		{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1, EffectiveAt: "bad"},
		{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1, ProrationBillingMode: "bad"},
		{ProviderSubscriptionID: "sub", ProductID: "prod", Quantity: 1, IdempotencyKey: "key", PaymentFailure: "bad"},
	} {
		if _, _, err := dodoPlanChangeRequest(invalid, true); err == nil {
			t.Fatal("expected plan request validation error")
		}
	}
	var response dodopayments.SubscriptionPreviewChangePlanResponse
	if err := json.Unmarshal([]byte(dodoPlanPreviewJSON), &response); err != nil {
		t.Fatal(err)
	}
	preview, err := dodoPlanChangePreview(response)
	if err != nil || preview.TotalAmount.IntPart() != 2200 || preview.TaxAmount == nil || preview.NextBillingDate == nil {
		t.Fatalf("preview = %#v, err = %v", preview, err)
	}
	for _, invalid := range []dodopayments.SubscriptionPreviewChangePlanResponse{
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.Summary.SettlementCurrency = ""
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.Summary.TotalAmount = -1
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.EffectiveAt = time.Time{}
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.NewPlan.Currency = ""
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.NewPlan.RecurringPreTaxAmount = -1
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.NewPlan.NextBillingDate = time.Time{}
			return v
		}(),
	} {
		if _, err := dodoPlanChangePreview(invalid); err == nil {
			t.Fatal("expected invalid plan preview")
		}
	}
	for _, invalid := range []dodopayments.SubscriptionPreviewChangePlanResponse{
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.LineItems[0].ProductID = ""
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.LineItems[0].Name = ""
			v.ImmediateCharge.LineItems[0].Description = ""
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.LineItems[0].UnitPrice = -1
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.LineItems[0].Quantity = 0
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.LineItems[0].Tax = -1
			return v
		}(),
		func() dodopayments.SubscriptionPreviewChangePlanResponse {
			v := response
			v.ImmediateCharge.LineItems[0].Currency = ""
			return v
		}(),
	} {
		if _, err := dodoPlanChangePreview(invalid); err == nil {
			t.Fatal("expected invalid plan line item")
		}
	}
	nanPreview := response
	nanPreview.ImmediateCharge.LineItems[0].ProrationFactor = math.NaN()
	if _, err := dodoPlanChangePreview(nanPreview); err == nil {
		t.Fatal("expected non-finite proration factor error")
	}
}

func TestDodoWebhookDecodeAndMetadataHelpers(t *testing.T) {
	if got := dodoMetadata(map[string]string{"key": "value"}); got["key"] == nil {
		t.Fatalf("metadata = %#v", got)
	}
	for _, payload := range []string{"", `{`, `{"type":"payment.succeeded"}`, `{"type":"payment.succeeded","data":"bad"}`} {
		if _, err := decodeWebhookPayload([]byte(payload)); err == nil {
			t.Fatalf("expected webhook decode error for %q", payload)
		}
	}
	envelope, err := decodeWebhookPayload([]byte(`{"type":"payment.succeeded","data":{"payment_id":"pay_1","amount":1}}`))
	if err != nil || envelope.Type != "payment.succeeded" || envelope.Data["payment_id"] != "pay_1" {
		t.Fatalf("envelope = %#v, err = %v", envelope, err)
	}
	if _, err := requireDodoIdempotencyKey(strings.Repeat("x", 256)); err == nil {
		t.Fatal("expected long idempotency key error")
	}
	if _, err := requireDodoResponseText(" ", "operation", "field"); err == nil {
		t.Fatal("expected response text error")
	}
	if err := dodoResponseError("operation", "field"); err == nil {
		t.Fatal("expected response error")
	}
}
