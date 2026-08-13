package bursar

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type outboxStoreStub struct {
	claim    func(context.Context, []string, int, int) ([]OutboxEvent, error)
	renew    func(context.Context, OutboxEvent, int) (bool, error)
	complete func(context.Context, OutboxEvent) (bool, error)
	fail     func(context.Context, OutboxEvent, string, int, int) (bool, error)
}

func (s *outboxStoreStub) Claim(ctx context.Context, topics []string, limit, lease int) ([]OutboxEvent, error) {
	if s.claim == nil {
		return nil, nil
	}
	return s.claim(ctx, topics, limit, lease)
}

func (s *outboxStoreStub) Renew(ctx context.Context, event OutboxEvent, lease int) (bool, error) {
	if s.renew == nil {
		return true, nil
	}
	return s.renew(ctx, event, lease)
}

func (s *outboxStoreStub) Complete(ctx context.Context, event OutboxEvent) (bool, error) {
	if s.complete == nil {
		return true, nil
	}
	return s.complete(ctx, event)
}

func (s *outboxStoreStub) Fail(ctx context.Context, event OutboxEvent, summary string, delay, limit int) (bool, error) {
	if s.fail == nil {
		return true, nil
	}
	return s.fail(ctx, event, summary, delay, limit)
}

type outboxHandlerStub struct {
	topics []string
	handle func(context.Context, OutboxEvent) error
}

func (h *outboxHandlerStub) Topics() []string { return append([]string(nil), h.topics...) }

func (h *outboxHandlerStub) Handle(ctx context.Context, event OutboxEvent) error {
	if h.handle == nil {
		return nil
	}
	return h.handle(ctx, event)
}

func TestOutboxWorkerDispatchesFailuresAndClaimLoss(t *testing.T) {
	events := []OutboxEvent{
		{EventID: "1", Topic: "usage", AttemptCount: 1},
		{EventID: "2", Topic: "usage", AttemptCount: 3},
		{EventID: "3", Topic: "usage", AttemptCount: 1},
	}
	var claimMu sync.Mutex
	claimed := false
	var failedSummary string
	var failedDelay int
	var outcomesMu sync.Mutex
	var outcomes []OutboxEventOutcome
	store := &outboxStoreStub{
		claim: func(_ context.Context, topics []string, limit, lease int) ([]OutboxEvent, error) {
			claimMu.Lock()
			defer claimMu.Unlock()
			if claimed {
				return nil, nil
			}
			claimed = true
			if len(topics) != 1 || topics[0] != "usage" || limit != 3 || lease != 60 {
				t.Fatalf("unexpected claim args: %#v %d %d", topics, limit, lease)
			}
			return append([]OutboxEvent(nil), events...), nil
		},
		complete: func(_ context.Context, event OutboxEvent) (bool, error) {
			return event.EventID != "3", nil
		},
		fail: func(_ context.Context, event OutboxEvent, summary string, delay, limit int) (bool, error) {
			if event.EventID != "2" || limit != 3 {
				t.Fatalf("unexpected failed event: %#v limit=%d", event, limit)
			}
			failedSummary, failedDelay = summary, delay
			return true, nil
		},
	}
	handler := &outboxHandlerStub{topics: []string{"usage"}, handle: func(_ context.Context, event OutboxEvent) error {
		if event.EventID == "2" {
			return errors.New("secret provider payload")
		}
		return nil
	}}
	worker, err := NewOutboxWorker(store, []OutboxHandler{handler}, OutboxWorkerOptions{
		BatchSize: 3, Concurrency: 3, RetryDelaySeconds: 2, MaxRetryDelaySeconds: 20, AttemptLimit: 3,
		OnEventOutcome: func(outcome OutboxEventOutcome) {
			outcomesMu.Lock()
			outcomes = append(outcomes, outcome)
			outcomesMu.Unlock()
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := worker.RunOnce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if result != (OutboxRunResult{Claimed: 3, Delivered: 1, Failed: 2, ClaimLost: 1}) {
		t.Fatalf("result = %#v", result)
	}
	if failedSummary != "outbox_delivery_failed:Error" {
		t.Fatalf("unsafe or unexpected summary = %q", failedSummary)
	}
	if failedDelay != 8 {
		t.Fatalf("retry delay = %d, want 8", failedDelay)
	}
	outcomesMu.Lock()
	defer outcomesMu.Unlock()
	if len(outcomes) != 3 {
		t.Fatalf("outcomes = %d, want 3", len(outcomes))
	}
	for _, outcome := range outcomes {
		if outcome.Summary != nil && *outcome.Summary == "secret provider payload" {
			t.Fatal("raw handler error leaked into outcome")
		}
	}
}

func TestOutboxWorkerHeartbeatClaimLoss(t *testing.T) {
	var completed atomic.Bool
	var renewed atomic.Int32
	claimed := atomic.Bool{}
	store := &outboxStoreStub{
		claim: func(context.Context, []string, int, int) ([]OutboxEvent, error) {
			if claimed.Swap(true) {
				return nil, nil
			}
			return []OutboxEvent{{EventID: "1", Topic: "slow", AttemptCount: 1}}, nil
		},
		renew: func(context.Context, OutboxEvent, int) (bool, error) {
			renewed.Add(1)
			return false, nil
		},
		complete: func(context.Context, OutboxEvent) (bool, error) {
			completed.Store(true)
			return true, nil
		},
	}
	var outcome OutboxEventOutcome
	worker, err := NewOutboxWorker(store, []OutboxHandler{&outboxHandlerStub{
		topics: []string{"slow"},
		handle: func(context.Context, OutboxEvent) error {
			time.Sleep(450 * time.Millisecond)
			return nil
		},
	}}, OutboxWorkerOptions{BatchSize: 1, Concurrency: 1, LeaseSeconds: 1, OnEventOutcome: func(value OutboxEventOutcome) { outcome = value }})
	if err != nil {
		t.Fatal(err)
	}
	result, err := worker.RunOnce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if renewed.Load() == 0 || completed.Load() {
		t.Fatalf("renewals=%d completed=%v", renewed.Load(), completed.Load())
	}
	if result.ClaimLost != 1 || outcome.Status != OutboxOutcomeClaimLost || outcome.ClaimLossPhase == nil || *outcome.ClaimLossPhase != OutboxClaimLossHeartbeat {
		t.Fatalf("heartbeat result=%#v outcome=%#v", result, outcome)
	}
}

func TestOutboxWorkerRunOnceIsSingleFlight(t *testing.T) {
	claimStarted := make(chan struct{})
	releaseClaim := make(chan struct{})
	var calls atomic.Int32
	store := &outboxStoreStub{claim: func(ctx context.Context, _ []string, _, _ int) ([]OutboxEvent, error) {
		if calls.Add(1) == 1 {
			close(claimStarted)
		}
		select {
		case <-releaseClaim:
			return nil, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}}
	worker, err := NewOutboxWorker(store, []OutboxHandler{&outboxHandlerStub{topics: []string{"one"}}}, OutboxWorkerOptions{})
	if err != nil {
		t.Fatal(err)
	}
	first := make(chan error, 1)
	go func() {
		_, runErr := worker.RunOnce(context.Background())
		first <- runErr
	}()
	<-claimStarted
	go func() {
		time.Sleep(50 * time.Millisecond)
		close(releaseClaim)
	}()
	if _, err := worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := <-first; err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatalf("claim calls = %d, want 1", calls.Load())
	}
}

func TestOutboxWorkerRuntimeLifecycle(t *testing.T) {
	worker, err := NewOutboxWorker(
		&outboxStoreStub{},
		[]OutboxHandler{&outboxHandlerStub{topics: []string{"one"}}},
		OutboxWorkerOptions{PollInterval: 10 * time.Millisecond},
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := worker.Health(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := worker.Close(ctx); err != nil {
		t.Fatal(err)
	}
	if err := worker.Health(context.Background()); err == nil {
		t.Fatal("closed worker reported healthy")
	}
	if err := worker.Start(context.Background()); err == nil {
		t.Fatal("closed worker restarted")
	}
}

func TestOutboxWorkerValidatesBounds(t *testing.T) {
	_, err := NewOutboxWorker(&outboxStoreStub{}, []OutboxHandler{&outboxHandlerStub{topics: []string{"one"}}}, OutboxWorkerOptions{
		RetryDelaySeconds:    10,
		MaxRetryDelaySeconds: 5,
	})
	if err == nil {
		t.Fatal("expected retry delay validation error")
	}
	if got := boundedOutboxRetryDelay(30, 3_600, 100); got != 3_600 {
		t.Fatalf("bounded retry delay = %d", got)
	}
}
