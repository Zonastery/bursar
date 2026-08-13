// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"math"
	"time"

	"github.com/cenkalti/backoff/v5"
)

// BursarRetryOptions configures bounded exponential backoff. Start with
// DefaultBursarRetryOptions and override the fields required by the caller;
// duration fields deliberately accept zero for an immediate retry/budget.
type BursarRetryOptions struct {
	// MaxAttempts includes the first call.
	MaxAttempts int
	// BaseDelay is the first delay between attempts.
	BaseDelay time.Duration
	// MaxDelay caps the delay between attempts.
	MaxDelay time.Duration
	// Factor multiplies the delay after each failed attempt.
	Factor float64
	// Jitter randomizes delays to avoid a thundering herd.
	Jitter bool
	// MaxElapsed bounds the complete operation, including attempts and waits.
	MaxElapsed time.Duration
	// ShouldRetry overrides the default Bursar error classification.
	ShouldRetry func(error) bool
	// OnRetry is called immediately before a retry is scheduled.
	OnRetry func(error, int, time.Duration)
}

// DefaultBursarRetryOptions returns the cross-SDK retry defaults.
func DefaultBursarRetryOptions() BursarRetryOptions {
	return BursarRetryOptions{
		MaxAttempts: 3,
		BaseDelay:   250 * time.Millisecond,
		MaxDelay:    2 * time.Second,
		Factor:      2,
		Jitter:      true,
		MaxElapsed:  30 * time.Second,
	}
}

func (options BursarRetryOptions) validate() error {
	if options.MaxAttempts < 1 {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry max attempts must be positive")
	}
	if options.BaseDelay < 0 {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry base delay must not be negative")
	}
	if options.MaxDelay < 0 {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry max delay must not be negative")
	}
	if options.MaxDelay < options.BaseDelay {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry max delay must be greater than or equal to base delay")
	}
	if math.IsNaN(options.Factor) || math.IsInf(options.Factor, 0) || options.Factor <= 0 {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry factor must be a finite positive number")
	}
	if options.MaxElapsed < 0 {
		return errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry max elapsed time must not be negative")
	}
	return nil
}

// RetryBursarOperation executes a read-only or replay-safe operation with
// bounded, cancellable exponential backoff. By default, only classified
// Bursar errors whose Retryable flag is true are attempted again. Mutation
// retries must reuse the exact same idempotency key.
//
// Omit options to use DefaultBursarRetryOptions. Supplying more than one options
// value is rejected so a call cannot silently ignore configuration.
func RetryBursarOperation[T any](ctx context.Context, operation func(context.Context) (T, error), supplied ...BursarRetryOptions) (T, error) {
	var zero T
	if ctx == nil {
		return zero, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry context is required")
	}
	if operation == nil {
		return zero, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry operation is required")
	}
	if len(supplied) > 1 {
		return zero, errorf(ErrorCodeConfig, ErrorCategoryInvalidRequest, "retry accepts at most one options value")
	}
	options := DefaultBursarRetryOptions()
	if len(supplied) == 1 {
		options = supplied[0]
	}
	if err := options.validate(); err != nil {
		return zero, err
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}

	shouldRetry := options.ShouldRetry
	if shouldRetry == nil {
		shouldRetry = IsRetryableError
	}

	var policy backoff.BackOff
	if options.BaseDelay == 0 && options.MaxDelay == 0 {
		policy = &backoff.ZeroBackOff{}
	} else {
		exponential := backoff.NewExponentialBackOff()
		exponential.InitialInterval = options.BaseDelay
		exponential.MaxInterval = options.MaxDelay
		exponential.Multiplier = options.Factor
		if !options.Jitter {
			exponential.RandomizationFactor = 0
		}
		policy = exponential
	}

	attempts := 0
	startedAt := time.Now()
	wrapped := func() (T, error) {
		if err := ctx.Err(); err != nil {
			return zero, backoff.Permanent(err)
		}
		attempts++
		result, err := operation(ctx)
		if err == nil {
			return result, nil
		}
		if !shouldRetry(err) || options.MaxElapsed == 0 || time.Since(startedAt) >= options.MaxElapsed {
			return result, backoff.Permanent(err)
		}
		return result, err
	}

	retryOptions := []backoff.RetryOption{
		backoff.WithBackOff(policy),
		backoff.WithMaxTries(uint(options.MaxAttempts)),
		backoff.WithMaxElapsedTime(options.MaxElapsed),
	}
	if options.OnRetry != nil {
		retryOptions = append(retryOptions, backoff.WithNotify(func(err error, delay time.Duration) {
			options.OnRetry(err, attempts+1, delay)
		}))
	}
	result, err := backoff.Retry(ctx, wrapped, retryOptions...)
	if permanent, ok := err.(*backoff.PermanentError); ok {
		return result, permanent.Unwrap()
	}
	return result, err
}
