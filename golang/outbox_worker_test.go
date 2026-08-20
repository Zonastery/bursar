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
	snapshot := worker.Snapshot()
	if snapshot.LastRun == nil || snapshot.LastRun.Result == nil {
		t.Fatalf("successful run snapshot = %#v", snapshot.LastRun)
	}
	snapshot.LastRun.Result.Claimed = 0
	if fresh := worker.Snapshot(); fresh.LastRun == nil || fresh.LastRun.Result == nil || fresh.LastRun.Result.Claimed != 3 {
		t.Fatalf("caller mutated retained worker snapshot: %#v", fresh.LastRun)
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

func TestOutboxWorkerStopIsIdempotentAndSnapshotsLocalState(t *testing.T) {
	worker, err := NewOutboxWorker(
		&outboxStoreStub{},
		[]OutboxHandler{&outboxHandlerStub{topics: []string{"one"}}},
		OutboxWorkerOptions{PollInterval: time.Hour},
	)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot := worker.Snapshot(); !snapshot.Configured || snapshot.Lifecycle != OutboxWorkerNotStarted {
		t.Fatalf("initial snapshot = %#v", snapshot)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if snapshot := worker.Snapshot(); snapshot.Lifecycle != OutboxWorkerRunning {
		t.Fatalf("running snapshot = %#v", snapshot)
	}
	if err := worker.Stop(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := worker.Stop(context.Background()); err != nil {
		t.Fatalf("second Stop() error = %v", err)
	}
	if snapshot := worker.Snapshot(); snapshot.Lifecycle != OutboxWorkerStopped {
		t.Fatalf("stopped snapshot = %#v", snapshot)
	}
	if err := worker.Close(context.Background()); err != nil {
		t.Fatalf("Close() after Stop() error = %v", err)
	}
}

func TestOutboxWorkerBackgroundRunDoesNotReplaceManualSnapshot(t *testing.T) {
	claimStarted := make(chan struct{})
	errorReported := make(chan struct{})
	var claimCalls atomic.Int32
	store := &outboxStoreStub{claim: func(context.Context, []string, int, int) ([]OutboxEvent, error) {
		switch claimCalls.Add(1) {
		case 1:
			return nil, errors.New("manual provider secret")
		case 2:
			close(claimStarted)
			return nil, errors.New("background provider secret")
		}
		return nil, nil
	}}
	worker, err := NewOutboxWorker(store, []OutboxHandler{&outboxHandlerStub{topics: []string{"one"}}}, OutboxWorkerOptions{
		PollInterval: time.Hour,
		OnError:      func(error) { close(errorReported) },
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := worker.RunOnce(context.Background()); err == nil {
		t.Fatal("manual run unexpectedly succeeded")
	}
	manual := worker.Snapshot()
	if manual.LastRun == nil || manual.LastRun.Source != "manual" || manual.LastRun.Error != "outbox_worker_failed:Error" {
		t.Fatalf("manual snapshot = %#v", manual.LastRun)
	}
	if manual.LastRun.Result != nil {
		t.Fatalf("failed manual run unexpectedly has a result: %#v", manual.LastRun.Result)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-claimStarted
	<-errorReported
	snapshot := worker.Snapshot()
	if snapshot.LastRun == nil || snapshot.LastRun.Source != "manual" {
		t.Fatalf("background run replaced manual snapshot: %#v", snapshot.LastRun)
	}
	if snapshot.LastError == nil || snapshot.LastError.Error != "outbox_worker_failed:Error" {
		t.Fatalf("background error snapshot = %#v", snapshot.LastError)
	}
	if err := worker.Stop(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestOutboxWorkerCloseDrainsActiveRun(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var claimed atomic.Bool
	var completed atomic.Bool
	var handlerCanceled atomic.Bool
	store := &outboxStoreStub{
		claim: func(context.Context, []string, int, int) ([]OutboxEvent, error) {
			if claimed.Swap(true) {
				return nil, nil
			}
			return []OutboxEvent{{EventID: "drain", Topic: "drain", AttemptCount: 1}}, nil
		},
		complete: func(context.Context, OutboxEvent) (bool, error) {
			completed.Store(true)
			return true, nil
		},
	}
	worker, err := NewOutboxWorker(store, []OutboxHandler{&outboxHandlerStub{
		topics: []string{"drain"},
		handle: func(ctx context.Context, _ OutboxEvent) error {
			close(started)
			select {
			case <-release:
				return nil
			case <-ctx.Done():
				handlerCanceled.Store(true)
				return ctx.Err()
			}
		},
	}}, OutboxWorkerOptions{BatchSize: 1, Concurrency: 1, PollInterval: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-started

	closeResult := make(chan error, 1)
	go func() { closeResult <- worker.Close(context.Background()) }()
	select {
	case err := <-closeResult:
		t.Fatalf("Close returned before active delivery was released: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	close(release)
	if err := <-closeResult; err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if !completed.Load() {
		t.Fatal("active event was not completed")
	}
	if handlerCanceled.Load() {
		t.Fatal("active handler context was canceled during graceful close")
	}
}

func TestOutboxWorkerCloseDeadlineCancelsActiveRun(t *testing.T) {
	started := make(chan struct{})
	handlerDone := make(chan struct{})
	var claimed atomic.Bool
	var handlerCanceled atomic.Bool
	store := &outboxStoreStub{
		claim: func(context.Context, []string, int, int) ([]OutboxEvent, error) {
			if claimed.Swap(true) {
				return nil, nil
			}
			return []OutboxEvent{{EventID: "deadline", Topic: "deadline", AttemptCount: 1}}, nil
		},
	}
	worker, err := NewOutboxWorker(store, []OutboxHandler{&outboxHandlerStub{
		topics: []string{"deadline"},
		handle: func(ctx context.Context, _ OutboxEvent) error {
			close(started)
			defer close(handlerDone)
			<-ctx.Done()
			handlerCanceled.Store(true)
			return ctx.Err()
		},
	}}, OutboxWorkerOptions{BatchSize: 1, Concurrency: 1, PollInterval: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if err := worker.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-started
	closeContext, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := worker.Close(closeContext); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Close() error = %v, want deadline exceeded", err)
	}
	select {
	case <-handlerDone:
	case <-time.After(time.Second):
		t.Fatal("active handler did not observe forced cancellation")
	}
	if !handlerCanceled.Load() {
		t.Fatal("active handler did not receive cancellation")
	}
	if err := worker.Close(context.Background()); err != nil {
		t.Fatalf("second Close() error = %v", err)
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
