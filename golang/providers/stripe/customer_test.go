// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package stripe

import (
	"io"
	"net/http"
	"strings"
	"testing"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

type stripeRoundTripper func(*http.Request) (*http.Response, error)

func (fn stripeRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}

func TestCreateCustomerThreadsStripeIdempotencyHeader(t *testing.T) {
	var gotHeader string
	client := &http.Client{Transport: stripeRoundTripper(func(request *http.Request) (*http.Response, error) {
		gotHeader = request.Header.Get("Idempotency-Key")
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"id":"cus_1","object":"customer","email":"buyer@example.com","name":"Buyer"}`))}, nil
	})}
	p, err := New(Options{APIKey: "sk_test", WebhookSecret: "whsec_test", HTTPClient: client})
	if err != nil {
		t.Fatal(err)
	}
	id, err := p.CreateCustomer(t.Context(), bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer-op-1"})
	if err != nil {
		t.Fatal(err)
	}
	if id != "cus_1" || gotHeader != "customer-op-1" {
		t.Fatalf("unexpected result/header: %q, %q", id, gotHeader)
	}
}

func TestCreateCustomerRequiresStripeStableKey(t *testing.T) {
	p, err := New(Options{APIKey: "sk_test", WebhookSecret: "whsec_test"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := p.CreateCustomer(t.Context(), bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer"}); err == nil {
		t.Fatal("expected idempotency-key validation")
	}
}
