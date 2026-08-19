package bursar

import (
	"errors"
	"fmt"
	"testing"
)

func TestBursarErrorPreservesClassificationThroughWrapping(t *testing.T) {
	want := NewStoreUnavailableError("database unavailable", nil)
	wrapped := fmt.Errorf("charge usage: %w", want)
	got, ok := AsBursarError(wrapped)
	if !ok || got.Code != ErrorCodeStoreUnavailable || !IsRetryableError(wrapped) {
		t.Fatalf("wrapped classification not preserved: %#v", got)
	}
	if !errors.Is(want, &BursarError{Code: ErrorCodeStoreUnavailable}) {
		t.Fatal("errors.Is does not match error code")
	}
}

func TestCreditBusinessErrorMatchesStoreCodeTaxonomy(t *testing.T) {
	tests := []struct {
		name     string
		code     string
		wantCode ErrorCode
		wantKind ErrorCategory
	}{
		{name: "concurrency alias", code: "concurrency_limit", wantCode: ErrorCodeConcurrencyLimitReached, wantKind: ErrorCategoryRateLimited},
		{name: "not found alias", code: "not_found", wantCode: ErrorCodeLeaseNotFound, wantKind: ErrorCategoryNotFound},
		{name: "settled lease", code: "settled_lease", wantCode: ErrorCodeLeaseNotFound, wantKind: ErrorCategoryNotFound},
		{name: "settlement conflict", code: "settlement_conflict", wantCode: ErrorCodeStore, wantKind: ErrorCategoryConflict},
		{name: "missing quota measure", code: "missing_quota_measure", wantCode: ErrorCodeConfig, wantKind: ErrorCategoryInvalidRequest},
		{name: "invalid measure", code: "invalid_measure", wantCode: ErrorCodeConfig, wantKind: ErrorCategoryInvalidRequest},
		{name: "policy mismatch", code: "policy_mismatch", wantCode: ErrorCodeConfig, wantKind: ErrorCategoryInvalidRequest},
		{name: "invalid amount", code: "invalid_amount", wantCode: ErrorCodeConfig, wantKind: ErrorCategoryInvalidRequest},
		{name: "invalid request", code: "invalid_request", wantCode: ErrorCodeConfig, wantKind: ErrorCategoryInvalidRequest},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, ok := AsBursarError(creditBusinessError("settle", "account-1", test.code))
			if !ok {
				t.Fatal("creditBusinessError() did not return a BursarError")
			}
			if got.Code != test.wantCode || got.Category != test.wantKind {
				t.Fatalf("classification = %s/%s, want %s/%s", got.Code, got.Category, test.wantCode, test.wantKind)
			}
		})
	}
}

func TestPublicErrorMappingsAreCompleteAndDetached(t *testing.T) {
	tests := []struct {
		category ErrorCategory
		status   int
		message  string
	}{
		{ErrorCategoryInvalidRequest, 400, "The billing request is invalid."},
		{ErrorCategoryPaymentRequired, 402, "Payment is required to continue."},
		{ErrorCategoryForbidden, 403, "Your current plan does not allow this operation."},
		{ErrorCategoryNotFound, 404, "The requested billing resource was not found."},
		{ErrorCategoryConflict, 409, "The request conflicts with the current billing state."},
		{ErrorCategoryRateLimited, 429, "A billing or usage limit has been reached."},
		{ErrorCategoryUnavailable, 503, "Billing service is temporarily unavailable. Please try again."},
		{ErrorCategoryInternal, 500, "Billing service is temporarily unavailable. Please try again."},
	}
	for _, test := range tests {
		t.Run(string(test.category), func(t *testing.T) {
			err := NewError("private diagnostic", ErrorOptions{Category: test.category})
			if got := ErrorHTTPStatus(err); got != test.status {
				t.Fatalf("HTTP status = %d, want %d", got, test.status)
			}
			if got := PublicErrorMessage(err); got != test.message {
				t.Fatalf("public message = %q", got)
			}
		})
	}
	if ErrorHTTPStatus(errors.New("unclassified")) != 500 || PublicErrorMessage(errors.New("unclassified")) != "Billing service is temporarily unavailable. Please try again." {
		t.Fatal("unclassified error did not use the safe fallback")
	}

	cause := errors.New("database secret")
	details := map[string]any{"operation": "charge"}
	err := NewError("safe", ErrorOptions{Cause: cause, Details: details, Retryable: true, Indeterminate: true})
	details["operation"] = "mutated"
	serialized := err.Serialize()
	serialized.Details["operation"] = "serialized mutation"
	if err.Details["operation"] != "charge" || !errors.Is(err, cause) {
		t.Fatalf("structured error was not detached: %#v", err)
	}
	var nilError *BursarError
	if nilError.Error() != "<nil>" || nilError.Unwrap() != nil || nilError.Serialize().Code != ErrorCodeBursar {
		t.Fatal("nil BursarError boundary is not stable")
	}
}
