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
