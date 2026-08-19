package googleadk

import (
	"context"
	"errors"
	"iter"
	"sync"
	"testing"
	"time"

	"github.com/Zonastery/bursar/golang/v2"
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/model"
	"google.golang.org/adk/v2/session"
	"google.golang.org/genai"
)

type fakeState struct {
	mu        sync.Mutex
	values    map[string]any
	setErr    error
	ignoreSet bool
}

func newFakeState() *fakeState { return &fakeState{values: make(map[string]any)} }

func (s *fakeState) Get(key string) (any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	value, ok := s.values[key]
	if !ok {
		return nil, session.ErrStateKeyNotExist
	}
	return value, nil
}

func (s *fakeState) Set(key string, value any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.setErr != nil {
		return s.setErr
	}
	if s.ignoreSet {
		return nil
	}
	s.values[key] = value
	return nil
}

func (s *fakeState) All() iter.Seq2[string, any] {
	return func(yield func(string, any) bool) {
		s.mu.Lock()
		copyValues := make(map[string]any, len(s.values))
		for key, value := range s.values {
			copyValues[key] = value
		}
		s.mu.Unlock()
		for key, value := range copyValues {
			if !yield(key, value) {
				return
			}
		}
	}
}

type fakeSession struct {
	state *fakeState
}

func (s *fakeSession) ID() string                         { return "session-1" }
func (s *fakeSession) AppName() string                    { return "app" }
func (s *fakeSession) UserID() string                     { return "user-1" }
func (s *fakeSession) State() session.State               { return s.state }
func (s *fakeSession) Events() session.Events             { return nil }
func (s *fakeSession) LastUpdateTime() (result time.Time) { return result }

type fakeContext struct {
	agent.StrictContextMock
	sess *fakeSession
	id   string
}

func newFakeContext(state *fakeState, id string) *fakeContext {
	return &fakeContext{StrictContextMock: agent.NewStrictContextMock(context.Background()), sess: &fakeSession{state: state}, id: id}
}

func (c *fakeContext) Session() session.Session { return c.sess }
func (c *fakeContext) InvocationID() string     { return c.id }

type fakeOperation struct {
	leaseID    string
	mu         sync.Mutex
	settled    []bursar.UsageMetrics
	release    int
	settleErr  error
	releaseErr error
}

func (o *fakeOperation) LeaseID() string { return o.leaseID }
func (o *fakeOperation) SettleUsage(_ context.Context, metrics bursar.UsageMetrics) (bursar.DeductionResult, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.settleErr != nil {
		return bursar.DeductionResult{}, o.settleErr
	}
	o.settled = append(o.settled, metrics)
	return bursar.DeductionResult{UsageChargeID: "usage-1", Amount: bursar.MustAmount("1")}, nil
}
func (o *fakeOperation) Release(context.Context) (bursar.ReleaseResult, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.releaseErr != nil {
		return bursar.ReleaseResult{}, o.releaseErr
	}
	o.release++
	return bursar.ReleaseResult{LeaseID: o.leaseID, Released: true}, nil
}

type fakeCredits struct {
	mu          sync.Mutex
	operation   *fakeOperation
	beginErr    error
	resumeErr   error
	releaseErr  error
	resumes     int
	releases    int
	lastUserID  string
	lastOptions bursar.BeginBilledUsageOperationOptions
}

func (c *fakeCredits) BeginBilledUsageOperation(_ context.Context, userID string, options bursar.BeginBilledUsageOperationOptions) (Operation, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.beginErr != nil {
		return nil, c.beginErr
	}
	c.lastUserID = userID
	c.lastOptions = options
	if c.operation == nil {
		c.operation = &fakeOperation{leaseID: "lease-1"}
	}
	return c.operation, nil
}
func (c *fakeCredits) ResumeBilledOperation(string, string, string, string, bursar.CreditMetadata) (Operation, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.resumes++
	if c.resumeErr != nil {
		return nil, c.resumeErr
	}
	if c.operation == nil {
		c.operation = &fakeOperation{leaseID: "lease-1"}
	}
	return c.operation, nil
}
func (c *fakeCredits) Release(_ context.Context, _, _ string) (bursar.ReleaseResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.releaseErr != nil {
		return bursar.ReleaseResult{}, c.releaseErr
	}
	c.releases++
	return bursar.ReleaseResult{Released: true}, nil
}

type fakeReceiptSource struct {
	receipt *bursar.ProviderReceipt
}

func (s *fakeReceiptSource) Begin() {}
func (s *fakeReceiptSource) Finish() *bursar.ProviderReceipt {
	receipt := s.receipt
	s.receipt = nil
	return receipt
}

func testOptions(credits any, source bursar.ProviderReceiptSource) Options {
	return Options{
		Credits: credits,
		Estimate: bursar.UsageMetrics{
			Operation: "completion",
			Measures: map[string]bursar.Amount{
				"calls": bursar.MustAmount("1"), "input_tokens": bursar.MustAmount("8"), "output_tokens": bursar.MustAmount("4"),
				"total_tokens": bursar.MustAmount("12"), "tool_calls": bursar.DecimalZero,
			},
			Dimensions: map[string]any{"model": "configured", "provider": "openrouter"},
		},
		ReceiptSource:      source,
		StateNamespace:     "test",
		OperationKeyPrefix: "test",
	}
}

func TestPluginReservesSettlesAndProtectsReceiptSchema(t *testing.T) {
	credits := &fakeCredits{}
	source := &fakeReceiptSource{}
	p, err := New(testOptions(credits, source))
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-1")
	if response, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "request-model"}); err != nil || response != nil {
		t.Fatalf("before model = (%v, %v)", response, err)
	}
	source.receipt = &bursar.ProviderReceipt{Metrics: bursar.UsageMetrics{
		Operation:  "provider-operation",
		Measures:   map[string]bursar.Amount{"input_tokens": bursar.MustAmount("11"), "latency_ms": bursar.MustAmount("900")},
		Dimensions: map[string]any{"model": "provider-model", "region": "iad"},
	}}
	response := &model.LLMResponse{ModelVersion: "response-model", Content: &genai.Content{Parts: []*genai.Part{{FunctionCall: &genai.FunctionCall{Name: "web_search"}}}}}
	if _, err := p.AfterModelCallback()(ctx, response, nil); err != nil {
		t.Fatal(err)
	}
	if len(credits.operation.settled) != 1 {
		t.Fatalf("settlements = %d, want 1", len(credits.operation.settled))
	}
	metrics := credits.operation.settled[0]
	if !metrics.Measures["input_tokens"].Equal(bursar.MustAmount("11")) || !metrics.Measures["tool_calls"].Equal(bursar.MustAmount("1")) {
		t.Fatalf("settled measures = %+v", metrics.Measures)
	}
	if _, ok := metrics.Measures["latency_ms"]; ok || metrics.Dimensions["region"] != nil {
		t.Fatalf("receipt exceeded estimate schema: %+v / %+v", metrics.Measures, metrics.Dimensions)
	}
	if value, err := state.Get("_bursar_model_leases:test:invocation-1"); err == nil {
		if entries, ok := value.([]map[string]any); !ok || len(entries) != 0 {
			t.Fatal("settled state was not cleared")
		}
	}
}

func TestPluginAdmissionDenialAndRelease(t *testing.T) {
	credits := &fakeCredits{beginErr: errors.New("quota exceeded")}
	p, err := New(testOptions(credits, nil))
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-1")
	response, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "model"})
	if err != nil || response == nil || response.ErrorCode != "ADMISSION_DENIED" {
		t.Fatalf("denial = (%+v, %v)", response, err)
	}

	credits.beginErr = nil
	if _, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "model"}); err != nil {
		t.Fatal(err)
	}
	_, _ = p.OnModelErrorCallback()(ctx, &model.LLMRequest{Model: "model"}, errors.New("provider failed"))
	if credits.releases != 1 {
		t.Fatalf("releases = %d, want 1", credits.releases)
	}
}

func TestPluginWaitsForFinalResponseAndReplaysReadySettlement(t *testing.T) {
	credits := &fakeCredits{}
	p, err := New(testOptions(credits, nil))
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-1")
	if _, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "model"}); err != nil {
		t.Fatal(err)
	}
	partial := &model.LLMResponse{Partial: true, Content: &genai.Content{Parts: []*genai.Part{{Text: "stream"}}}}
	if _, err := p.AfterModelCallback()(ctx, partial, nil); err != nil {
		t.Fatal(err)
	}
	if len(credits.operation.settled) != 0 {
		t.Fatal("partial response settled the lease")
	}
	// A ready entry survives a process boundary and is settled by BeforeRun.
	key := "_bursar_model_leases:test:invocation-1"
	value, err := state.Get(key)
	if err != nil {
		t.Fatal(err)
	}
	payload := value.([]map[string]any)
	payload[0]["metrics"] = encodeMetrics(bursar.UsageMetrics{Operation: "completion", Measures: map[string]bursar.Amount{"calls": bursar.MustAmount("1")}})
	if err := state.Set(key, payload); err != nil {
		t.Fatal(err)
	}
	if _, err := p.BeforeRunCallback()(ctx); err != nil {
		t.Fatal(err)
	}
	if credits.resumes != 1 || len(credits.operation.settled) != 1 {
		t.Fatalf("replay = resumes %d, settlements %d", credits.resumes, len(credits.operation.settled))
	}
}

func TestPluginRetainsReadyStateOnGenericSettlementFailure(t *testing.T) {
	credits := &fakeCredits{}
	p, err := New(testOptions(credits, nil))
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-1")
	if _, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "model"}); err != nil {
		t.Fatal(err)
	}
	key := "_bursar_model_leases:test:invocation-1"
	value, err := state.Get(key)
	if err != nil {
		t.Fatal(err)
	}
	payload := value.([]map[string]any)
	payload[0]["metrics"] = encodeMetrics(bursar.UsageMetrics{Operation: "completion", Measures: map[string]bursar.Amount{"calls": bursar.MustAmount("1")}})
	if err := state.Set(key, payload); err != nil {
		t.Fatal(err)
	}
	credits.operation.settleErr = errors.New("database transport failed")
	if _, err := p.BeforeRunCallback()(ctx); err != nil {
		t.Fatal(err)
	}
	value, err = state.Get(key)
	if err != nil || len(value.([]map[string]any)) != 1 {
		t.Fatal("generic settlement failure discarded durable ready state")
	}
}

func TestPluginReleasesLeaseWhenStatePersistenceFails(t *testing.T) {
	credits := &fakeCredits{}
	p, err := New(testOptions(credits, nil))
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	state.ignoreSet = true
	ctx := newFakeContext(state, "invocation-1")
	response, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "model"})
	if err != nil || response == nil || credits.operation == nil || credits.operation.release != 1 {
		t.Fatalf("state failure = response %v, error %v, operation %+v", response, err, credits.operation)
	}
}
