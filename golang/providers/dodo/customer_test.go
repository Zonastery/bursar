// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package dodo

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	bursar "github.com/Zonastery/bursar/golang/v2"
)

type dodoRoundTripper func(*http.Request) (*http.Response, error)

func (fn dodoRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}

func TestCreateCustomerThreadsDodoIdempotencyHeader(t *testing.T) {
	var gotHeader string
	var gotHost string
	client := &http.Client{Transport: dodoRoundTripper(func(request *http.Request) (*http.Response, error) {
		gotHeader = request.Header.Get("Idempotency-Key")
		gotHost = request.URL.Host
		body, _ := io.ReadAll(request.Body)
		var payload map[string]any
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatal(err)
		}
		if payload["email"] != "buyer@example.com" || payload["name"] != "Buyer" {
			t.Fatalf("unexpected request: %s", body)
		}
		header := make(http.Header)
		header.Set("Content-Type", "application/json")
		return &http.Response{StatusCode: http.StatusOK, Header: header, Body: io.NopCloser(strings.NewReader(`{"business_id":"bus_1","customer_id":"cus_1","email":"buyer@example.com","name":"Buyer","created_at":"2026-07-18T05:15:24Z","metadata":{}}`)), Request: request}, nil
	})}
	p, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey, HTTPClient: client})
	if err != nil {
		t.Fatal(err)
	}
	id, err := p.CreateCustomer(context.Background(), bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer", IdempotencyKey: "customer-op-1"})
	if err != nil {
		t.Fatalf("%v: %v", err, errors.Unwrap(err))
	}
	if id != "cus_1" || gotHeader != "customer-op-1" || gotHost != "test.dodopayments.com" {
		t.Fatalf("unexpected result/header/environment: %q, %q, %q", id, gotHeader, gotHost)
	}
}

func TestNewRejectsInvalidDodoEnvironment(t *testing.T) {
	if _, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey, Environment: bursar.ProviderEnvironment("staging")}); err == nil {
		t.Fatal("expected invalid environment error")
	}
	provider, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey, Environment: bursar.ProviderEnvironmentSandbox})
	if err != nil {
		t.Fatal(err)
	}
	if provider.ProviderEnvironment() != bursar.ProviderEnvironmentSandbox {
		t.Fatalf("unexpected environment: %q", provider.ProviderEnvironment())
	}
}

func TestCreateCustomerRequiresDodoStableKey(t *testing.T) {
	p, err := New(Options{APIKey: "test", WebhookKey: dodoTestWebhookKey})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := p.CreateCustomer(t.Context(), bursar.CreateCustomerRequest{Email: "buyer@example.com", Name: "Buyer"}); err == nil {
		t.Fatal("expected idempotency-key validation")
	}
}
