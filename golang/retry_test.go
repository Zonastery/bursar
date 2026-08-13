// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"math"
	"reflect"
	"testing"
	"time"
)

func immediateRetryOptions(maxAttempts int) BursarRetryOptions {
	return BursarRetryOptions{
		MaxAttempts: maxAttempts,
		BaseDelay:   0,
		MaxDelay:    0,
		Factor:      2,
		Jitter:      false,
		MaxElapsed:  time.Second,
	}
}

func TestRetryBursarOperationRetriesClassifiedErrors(t *testing.T) {
	retryable := NewStoreUnavailableError("database unavailable", errors.New("transport"))
	attempts := 0
	var nextAttempts []int
	var delays []time.Duration

	result, err := RetryBursarOperation(context.Background(), func(context.Context) (string, error) {
		attempts++
		if attempts < 3 {
			return "", retryable
		}
		return "committed", nil
	}, func() BursarRetryOptions {
		options := immediateRetryOptions(4)
		options.OnRetry = func(err error, nextAttempt int, delay time.Duration) {
			if err != retryable {
				t.Errorf("OnRetry error = %v, want original classified error", err)
			}
			nextAttempts = append(nextAttempts, nextAttempt)
			delays = append(delays, delay)
		}
		return options
	}())
	if err != nil {
		t.Fatalf("RetryBursarOperation() error = %v", err)
	}
	if result != "committed" || attempts != 3 {
		t.Fatalf("result/attempts = %q/%d, want committed/3", result, attempts)
	}
	if !reflect.DeepEqual(nextAttempts, []int{2, 3}) {
		t.Fatalf("next attempts = %v, want [2 3]", nextAttempts)
	}
	if !reflect.DeepEqual(delays, []time.Duration{0, 0}) {
		t.Fatalf("retry delays = %v, want immediate retries", delays)
	}
}

func TestRetryBursarOperationDoesNotRetryUnclassifiedOrNonRetryableErrors(t *testing.T) {
	for name, operationErr := range map[string]error{
		"plain":         errors.New("plain error"),
		"non-retryable": NewError("conflict", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict}),
	} {
		t.Run(name, func(t *testing.T) {
			attempts := 0
			callbacks := 0
			options := immediateRetryOptions(5)
			options.OnRetry = func(error, int, time.Duration) { callbacks++ }
			_, err := RetryBursarOperation(context.Background(), func(context.Context) (struct{}, error) {
				attempts++
				return struct{}{}, operationErr
			}, options)
			if err != operationErr {
				t.Fatalf("error = %v, want original %v", err, operationErr)
			}
			if attempts != 1 || callbacks != 0 {
				t.Fatalf("attempts/callbacks = %d/%d, want 1/0", attempts, callbacks)
			}
		})
	}
	operationErr := NewError("single attempt", ErrorOptions{Code: ErrorCodeCheckoutConflict, Category: ErrorCategoryConflict})
	_, err := RetryBursarOperation(context.Background(), func(context.Context) (int, error) {
		return 0, operationErr
	}, immediateRetryOptions(1))
	if err != operationErr {
		t.Fatalf("single-attempt error = %#v, want original error", err)
	}
}

func TestRetryBursarOperationHonorsOverrideAndAttemptBound(t *testing.T) {
	operationErr := errors.New("host-classified transient")
	attempts := 0
	options := immediateRetryOptions(3)
	options.ShouldRetry = func(err error) bool { return err == operationErr }

	_, err := RetryBursarOperation(context.Background(), func(context.Context) (int, error) {
		attempts++
		return attempts, operationErr
	}, options)
	if err != operationErr {
		t.Fatalf("error = %v, want original error", err)
	}
	if attempts != 3 {
		t.Fatalf("attempts = %d, want max-attempt bound 3", attempts)
	}
}

func TestRetryBursarOperationHonorsElapsedBudget(t *testing.T) {
	attempts := 0
	options := immediateRetryOptions(10)
	options.MaxElapsed = time.Millisecond

	_, err := RetryBursarOperation(context.Background(), func(context.Context) (int, error) {
		attempts++
		time.Sleep(2 * time.Millisecond)
		return 0, NewStoreUnavailableError("unavailable", nil)
	}, options)
	if err == nil {
		t.Fatal("RetryBursarOperation() error = nil, want retryable failure")
	}
	if attempts != 1 {
		t.Fatalf("attempts = %d, want elapsed budget to stop after first attempt", attempts)
	}
}

func TestRetryBursarOperationCancellationInterruptsBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	attempts := 0
	options := DefaultBursarRetryOptions()
	options.MaxAttempts = 10
	options.BaseDelay = 5 * time.Second
	options.MaxDelay = 5 * time.Second
	options.Jitter = false
	options.OnRetry = func(error, int, time.Duration) { cancel() }

	startedAt := time.Now()
	_, err := RetryBursarOperation(ctx, func(operationContext context.Context) (int, error) {
		attempts++
		if operationContext != ctx {
			t.Fatal("operation received a different context")
		}
		return 0, NewStoreUnavailableError("unavailable", nil)
	}, options)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v, want context.Canceled", err)
	}
	if attempts != 1 {
		t.Fatalf("attempts = %d, want 1", attempts)
	}
	if elapsed := time.Since(startedAt); elapsed > time.Second {
		t.Fatalf("cancellation took %s; pending backoff was not interrupted", elapsed)
	}
}

func TestRetryBursarOperationValidatesConfiguration(t *testing.T) {
	tests := map[string]BursarRetryOptions{
		"attempts":    {MaxAttempts: 0, Factor: 1},
		"base delay":  {MaxAttempts: 1, BaseDelay: -1, Factor: 1},
		"max delay":   {MaxAttempts: 1, BaseDelay: 2, MaxDelay: 1, Factor: 1},
		"factor nan":  {MaxAttempts: 1, Factor: math.NaN()},
		"factor zero": {MaxAttempts: 1, Factor: 0},
		"elapsed":     {MaxAttempts: 1, Factor: 1, MaxElapsed: -1},
	}
	for name, options := range tests {
		t.Run(name, func(t *testing.T) {
			_, err := RetryBursarOperation(context.Background(), func(context.Context) (int, error) { return 1, nil }, options)
			classified, ok := AsBursarError(err)
			if !ok || classified.Code != ErrorCodeConfig || classified.Category != ErrorCategoryInvalidRequest {
				t.Fatalf("error = %#v, want classified config error", err)
			}
		})
	}
}

func TestRetryBursarOperationRejectsNilInputsAndMultipleOptions(t *testing.T) {
	valid := immediateRetryOptions(1)
	if _, err := RetryBursarOperation[int](nil, func(context.Context) (int, error) { return 1, nil }); err == nil {
		t.Fatal("nil context error = nil")
	}
	if _, err := RetryBursarOperation[int](context.Background(), nil); err == nil {
		t.Fatal("nil operation error = nil")
	}
	if _, err := RetryBursarOperation(context.Background(), func(context.Context) (int, error) { return 1, nil }, valid, valid); err == nil {
		t.Fatal("multiple options error = nil")
	}
}
