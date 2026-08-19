// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

type OutboxEventOutcomeStatus string

const (
	OutboxOutcomeDelivered      OutboxEventOutcomeStatus = "delivered"
	OutboxOutcomeDeliveryFailed OutboxEventOutcomeStatus = "delivery_failed"
	OutboxOutcomeClaimLost      OutboxEventOutcomeStatus = "claim_lost"
)

type OutboxClaimLossPhase string

const (
	OutboxClaimLossHeartbeat OutboxClaimLossPhase = "heartbeat"
	OutboxClaimLossComplete  OutboxClaimLossPhase = "complete"
	OutboxClaimLossFail      OutboxClaimLossPhase = "fail"
)

type OutboxEventOutcome struct {
	Topic             string                   `json:"topic"`
	AttemptCount      int                      `json:"attempt_count"`
	Status            OutboxEventOutcomeStatus `json:"status"`
	Summary           *string                  `json:"summary,omitempty"`
	Duration          time.Duration            `json:"duration"`
	RetryDelaySeconds *int                     `json:"retry_delay_seconds,omitempty"`
	DeadLettered      bool                     `json:"dead_lettered"`
	ClaimLossPhase    *OutboxClaimLossPhase    `json:"claim_loss_phase,omitempty"`
}

type OutboxWorkerOptions struct {
	BatchSize            int
	Concurrency          int
	LeaseSeconds         int
	PollInterval         time.Duration
	RetryDelaySeconds    int
	MaxRetryDelaySeconds int
	AttemptLimit         int
	OnError              func(error)
	OnEventOutcome       func(OutboxEventOutcome)
}

func (o OutboxWorkerOptions) normalized() (OutboxWorkerOptions, error) {
	if o.BatchSize == 0 {
		o.BatchSize = 100
	}
	if o.Concurrency == 0 {
		o.Concurrency = 4
	}
	if o.LeaseSeconds == 0 {
		o.LeaseSeconds = 60
	}
	if o.PollInterval == 0 {
		o.PollInterval = time.Second
	}
	if o.RetryDelaySeconds == 0 {
		o.RetryDelaySeconds = 30
	}
	if o.MaxRetryDelaySeconds == 0 {
		o.MaxRetryDelaySeconds = 3_600
	}
	if o.AttemptLimit == 0 {
		o.AttemptLimit = 10
	}
	if o.BatchSize < 1 || o.BatchSize > 1_000 {
		return o, outboxConfigError("batch size must be between 1 and 1000")
	}
	if o.Concurrency < 1 || o.Concurrency > 100 {
		return o, outboxConfigError("concurrency must be between 1 and 100")
	}
	if o.LeaseSeconds < 1 || o.LeaseSeconds > 3_600 {
		return o, outboxConfigError("lease seconds must be between 1 and 3600")
	}
	if o.PollInterval < 10*time.Millisecond || o.PollInterval > time.Hour {
		return o, outboxConfigError("poll interval must be between 10ms and 1h")
	}
	if o.RetryDelaySeconds < 1 || o.RetryDelaySeconds > 86_400 {
		return o, outboxConfigError("retry delay seconds must be between 1 and 86400")
	}
	if o.MaxRetryDelaySeconds < 1 || o.MaxRetryDelaySeconds > 86_400 {
		return o, outboxConfigError("maximum retry delay seconds must be between 1 and 86400")
	}
	if o.MaxRetryDelaySeconds < o.RetryDelaySeconds {
		return o, outboxConfigError("maximum retry delay must be at least the base retry delay")
	}
	if o.AttemptLimit < 1 || o.AttemptLimit > 100 {
		return o, outboxConfigError("attempt limit must be between 1 and 100")
	}
	return o, nil
}

type OutboxRunResult struct {
	Claimed   int `json:"claimed"`
	Delivered int `json:"delivered"`
	Failed    int `json:"failed"`
	ClaimLost int `json:"claim_lost"`
}

type outboxActiveRun struct {
	done   chan struct{}
	cancel context.CancelFunc
	result OutboxRunResult
	err    error
}

// OutboxWorker is a bounded, leased dispatcher. It is also a RuntimeComponent:
// Start begins polling, Flush runs one bounded pass, and Close waits for active
// delivery work after cancelling the worker-owned context.
type OutboxWorker struct {
	store    OutboxStore
	handlers map[string][]OutboxHandler
	topics   []string
	options  OutboxWorkerOptions

	mu         sync.Mutex
	started    bool
	stopped    bool
	loopCancel context.CancelFunc
	loopDone   chan struct{}
	active     *outboxActiveRun
}

var _ RuntimeComponent = (*OutboxWorker)(nil)
var _ RuntimeHealthChecker = (*OutboxWorker)(nil)

func NewOutboxWorker(store OutboxStore, handlers []OutboxHandler, options OutboxWorkerOptions) (*OutboxWorker, error) {
	if store == nil {
		return nil, outboxConfigError("outbox store is required")
	}
	if len(handlers) == 0 {
		return nil, outboxConfigError("OutboxWorker requires at least one handler")
	}
	normalized, err := options.normalized()
	if err != nil {
		return nil, err
	}
	byTopic := make(map[string][]OutboxHandler)
	for _, handler := range handlers {
		if handler == nil {
			return nil, outboxConfigError("outbox handlers must not be nil")
		}
		topics := handler.Topics()
		if len(topics) == 0 {
			return nil, outboxConfigError("outbox handlers must declare at least one topic")
		}
		for _, topic := range topics {
			topic = strings.TrimSpace(topic)
			if topic == "" || len(topic) > 255 {
				return nil, outboxConfigError("outbox topics must contain between 1 and 255 characters")
			}
			byTopic[topic] = append(byTopic[topic], handler)
		}
	}
	if len(byTopic) > 64 {
		return nil, outboxConfigError("OutboxWorker supports at most 64 topics")
	}
	topics := make([]string, 0, len(byTopic))
	for topic := range byTopic {
		topics = append(topics, topic)
	}
	sort.Strings(topics)
	return &OutboxWorker{store: store, handlers: byTopic, topics: topics, options: normalized}, nil
}

func (w *OutboxWorker) Start(ctx context.Context) error {
	if w == nil {
		return outboxConfigError("outbox worker is not initialized")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	w.mu.Lock()
	if w.stopped {
		w.mu.Unlock()
		return NewError("OutboxWorker cannot be restarted after close", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if w.started {
		w.mu.Unlock()
		return nil
	}
	loopCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	done := make(chan struct{})
	w.started = true
	w.loopCancel = cancel
	w.loopDone = done
	w.mu.Unlock()
	go w.runLoop(loopCtx, done)
	return nil
}

func (w *OutboxWorker) RunOnce(ctx context.Context) (OutboxRunResult, error) {
	if w == nil {
		return OutboxRunResult{}, outboxConfigError("outbox worker is not initialized")
	}
	return w.runOnce(ctx, false)
}

func (w *OutboxWorker) runOnce(ctx context.Context, detachFromParent bool) (OutboxRunResult, error) {
	w.mu.Lock()
	if w.stopped {
		w.mu.Unlock()
		return OutboxRunResult{}, NewError("OutboxWorker has been closed", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if current := w.active; current != nil {
		w.mu.Unlock()
		return waitOutboxRun(ctx, current)
	}
	runContext := ctx
	if detachFromParent {
		runContext = context.WithoutCancel(ctx)
	}
	runContext, cancel := context.WithCancel(runContext)
	current := &outboxActiveRun{done: make(chan struct{}), cancel: cancel}
	w.active = current
	w.mu.Unlock()

	current.result, current.err = w.dispatchOnce(runContext)
	cancel()
	w.mu.Lock()
	if w.active == current {
		w.active = nil
	}
	close(current.done)
	w.mu.Unlock()
	return current.result, current.err
}

func waitOutboxRun(ctx context.Context, run *outboxActiveRun) (OutboxRunResult, error) {
	select {
	case <-ctx.Done():
		return OutboxRunResult{}, ctx.Err()
	case <-run.done:
		return run.result, run.err
	}
}

func (w *OutboxWorker) Flush(ctx context.Context) error {
	_, err := w.RunOnce(ctx)
	return err
}

func (w *OutboxWorker) Close(ctx context.Context) error {
	if w == nil {
		return nil
	}
	w.mu.Lock()
	if !w.stopped {
		w.stopped = true
		if w.loopCancel != nil {
			w.loopCancel()
		}
	}
	loopDone := w.loopDone
	active := w.active
	w.mu.Unlock()
	waitForRun := func() error {
		if active == nil {
			return nil
		}
		select {
		case <-active.done:
			return active.err
		case <-ctx.Done():
			active.cancel()
			return ctx.Err()
		}
	}
	var failures []error
	if err := waitForRun(); err != nil {
		if ctx.Err() != nil {
			return err
		}
		failures = append(failures, err)
	}
	if loopDone != nil {
		select {
		case <-loopDone:
		case <-ctx.Done():
			if active != nil {
				active.cancel()
			}
			return ctx.Err()
		}
	}
	return errors.Join(failures...)
}

func (w *OutboxWorker) Health(context.Context) error {
	if w == nil {
		return NewError("outbox worker is not initialized", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.stopped {
		return NewError("outbox worker is closed", ErrorOptions{Code: ErrorCodeStoreClosed, Category: ErrorCategoryUnavailable})
	}
	if !w.started {
		return NewError("outbox worker is not started", ErrorOptions{Code: ErrorCodeStore, Category: ErrorCategoryUnavailable})
	}
	return nil
}

func (w *OutboxWorker) runLoop(ctx context.Context, done chan struct{}) {
	defer close(done)
	for {
		if _, err := w.runOnce(ctx, true); err != nil && !errors.Is(err, context.Canceled) {
			w.reportError(err)
		}
		timer := time.NewTimer(w.options.PollInterval)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				<-timer.C
			}
			return
		case <-timer.C:
		}
	}
}

func (w *OutboxWorker) dispatchOnce(ctx context.Context) (OutboxRunResult, error) {
	var result OutboxRunResult
	remaining := w.options.BatchSize
	for remaining > 0 {
		available := min(w.options.Concurrency, remaining)
		events, err := w.store.Claim(ctx, append([]string(nil), w.topics...), available, w.options.LeaseSeconds)
		if err != nil {
			return result, err
		}
		if len(events) == 0 {
			break
		}
		if len(events) > available {
			return result, NewStoreError(fmt.Sprintf("outbox store returned %d events for %d available slots", len(events), available), ErrorOptions{})
		}
		result.Claimed += len(events)
		remaining -= len(events)
		outcomes := make(chan OutboxEventOutcomeStatus, len(events))
		for _, event := range events {
			event := event
			go func() { outcomes <- w.dispatchEvent(ctx, event) }()
		}
		for range events {
			outcome := <-outcomes
			if outcome == OutboxOutcomeDelivered {
				result.Delivered++
			} else {
				result.Failed++
				if outcome == OutboxOutcomeClaimLost {
					result.ClaimLost++
				}
			}
		}
		if len(events) < available {
			break
		}
	}
	return result, nil
}

func (w *OutboxWorker) dispatchEvent(ctx context.Context, event OutboxEvent) OutboxEventOutcomeStatus {
	started := time.Now()
	heartbeat := newOutboxHeartbeat(ctx, w.store, event, w.options.LeaseSeconds)
	handlers := w.handlers[event.Topic]
	var deliveryErr error
	if len(handlers) == 0 {
		deliveryErr = errors.New("outbox topic has no handler")
	} else {
		failures := make(chan error, len(handlers))
		for _, handler := range handlers {
			handler := handler
			go func() { failures <- invokeOutboxHandler(ctx, handler, event) }()
		}
		for range handlers {
			if err := <-failures; err != nil && deliveryErr == nil {
				deliveryErr = err
			}
		}
	}
	heartbeatLost, heartbeatSummary := heartbeat.stop()
	if heartbeatLost {
		return w.claimLost(event, OutboxClaimLossHeartbeat, heartbeatSummary, started)
	}
	if deliveryErr != nil {
		return w.failDelivery(ctx, event, deliveryErr, started)
	}
	completed, err := w.store.Complete(ctx, event)
	if err != nil || !completed {
		summary := persistedDiagnosticSummary(err, "outbox_claim_lost")
		return w.claimLost(event, OutboxClaimLossComplete, summary, started)
	}
	w.reportOutcome(OutboxEventOutcome{
		Topic: event.Topic, AttemptCount: event.AttemptCount, Status: OutboxOutcomeDelivered,
		Duration: max(time.Since(started), 0),
	})
	return OutboxOutcomeDelivered
}

func (w *OutboxWorker) failDelivery(ctx context.Context, event OutboxEvent, deliveryErr error, started time.Time) OutboxEventOutcomeStatus {
	summary := persistedDiagnosticSummary(deliveryErr, "outbox_delivery_failed")
	delay := boundedOutboxRetryDelay(w.options.RetryDelaySeconds, w.options.MaxRetryDelaySeconds, event.AttemptCount)
	failed, err := w.store.Fail(ctx, event, summary, delay, w.options.AttemptLimit)
	if err != nil || !failed {
		claimSummary := summary
		if err != nil {
			claimSummary = persistedDiagnosticSummary(err, "outbox_claim_lost")
		}
		return w.claimLost(event, OutboxClaimLossFail, claimSummary, started)
	}
	w.reportOutcome(OutboxEventOutcome{
		Topic: event.Topic, AttemptCount: event.AttemptCount, Status: OutboxOutcomeDeliveryFailed,
		Summary: &summary, Duration: max(time.Since(started), 0), RetryDelaySeconds: &delay,
		DeadLettered: event.AttemptCount >= w.options.AttemptLimit,
	})
	return OutboxOutcomeDeliveryFailed
}

func (w *OutboxWorker) claimLost(event OutboxEvent, phase OutboxClaimLossPhase, summary string, started time.Time) OutboxEventOutcomeStatus {
	if summary == "" {
		summary = persistedDiagnosticSummary(nil, "outbox_claim_lost")
	}
	w.reportOutcome(OutboxEventOutcome{
		Topic: event.Topic, AttemptCount: event.AttemptCount, Status: OutboxOutcomeClaimLost,
		Summary: &summary, Duration: max(time.Since(started), 0), ClaimLossPhase: &phase,
	})
	return OutboxOutcomeClaimLost
}

func (w *OutboxWorker) reportError(err error) {
	if w.options.OnError == nil {
		return
	}
	defer func() { _ = recover() }()
	w.options.OnError(err)
}

func (w *OutboxWorker) reportOutcome(outcome OutboxEventOutcome) {
	if w.options.OnEventOutcome == nil {
		return
	}
	defer func() { _ = recover() }()
	w.options.OnEventOutcome(outcome)
}

type outboxHeartbeat struct {
	stopOnce sync.Once
	stopCh   chan struct{}
	done     chan struct{}
	lost     bool
	summary  string
}

func newOutboxHeartbeat(ctx context.Context, store OutboxStore, event OutboxEvent, leaseSeconds int) *outboxHeartbeat {
	heartbeat := &outboxHeartbeat{stopCh: make(chan struct{}), done: make(chan struct{})}
	interval := time.Duration(leaseSeconds) * time.Second / 3
	if interval < 100*time.Millisecond {
		interval = 100 * time.Millisecond
	}
	go func() {
		defer close(heartbeat.done)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-heartbeat.stopCh:
				return
			case <-ctx.Done():
				heartbeat.lost = true
				heartbeat.summary = persistedDiagnosticSummary(ctx.Err(), "outbox_claim_lost")
				return
			case <-ticker.C:
				renewed, err := store.Renew(ctx, event, leaseSeconds)
				if err != nil || !renewed {
					heartbeat.lost = true
					heartbeat.summary = persistedDiagnosticSummary(err, "outbox_claim_lost")
					return
				}
			}
		}
	}()
	return heartbeat
}

func (h *outboxHeartbeat) stop() (bool, string) {
	h.stopOnce.Do(func() { close(h.stopCh) })
	<-h.done
	return h.lost, h.summary
}

func invokeOutboxHandler(ctx context.Context, handler OutboxHandler, event OutboxEvent) (err error) {
	defer func() {
		if recover() != nil {
			err = errors.New("outbox handler panicked")
		}
	}()
	return handler.Handle(ctx, event)
}

func boundedOutboxRetryDelay(base, maximum, attempt int) int {
	delay := base
	for step := 1; step < max(attempt, 1) && delay < maximum; step++ {
		if delay > maximum/2 {
			return maximum
		}
		delay *= 2
	}
	return min(delay, maximum)
}

var diagnosticCodePattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,127}$`)

func persistedDiagnosticSummary(err error, fallback string) string {
	operation := sanitizeDiagnosticCode(fallback, "operation_failed")
	errorCode := "UnknownError"
	if err != nil {
		errorCode = "Error"
		if bursarErr, ok := AsBursarError(err); ok && diagnosticCodePattern.MatchString(string(bursarErr.Code)) {
			errorCode = string(bursarErr.Code)
		}
	}
	return operation + ":" + errorCode
}

func validatePersistedDiagnosticSummary(value string) (string, error) {
	parts := strings.Split(value, ":")
	if len(parts) != 2 || !diagnosticCodePattern.MatchString(parts[0]) || !diagnosticCodePattern.MatchString(parts[1]) {
		return "", outboxConfigError("outbox failure summary must contain two safe diagnostic codes")
	}
	return value, nil
}

func sanitizeDiagnosticCode(value, fallback string) string {
	value = strings.TrimSpace(value)
	var builder strings.Builder
	for _, character := range value {
		if builder.Len() >= 128 {
			break
		}
		if (character >= 'A' && character <= 'Z') || (character >= 'a' && character <= 'z') ||
			(character >= '0' && character <= '9') || character == '_' || character == '.' || character == '-' {
			builder.WriteRune(character)
		} else {
			builder.WriteByte('_')
		}
	}
	result := builder.String()
	if !diagnosticCodePattern.MatchString(result) {
		return fallback
	}
	return result
}

func outboxConfigError(message string) error {
	return NewError(message, ErrorOptions{Code: ErrorCodeConfig, Category: ErrorCategoryInvalidRequest})
}
