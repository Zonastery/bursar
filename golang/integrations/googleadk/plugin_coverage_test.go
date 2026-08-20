package googleadk

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Zonastery/bursar/golang/v2"
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/model"
	"google.golang.org/adk/v2/session"
	"google.golang.org/genai"
)

type emptyInvocationContext struct{ agent.StrictContextMock }

func (c *emptyInvocationContext) Session() session.Session { return nil }
func (c *emptyInvocationContext) InvocationID() string     { return "" }

type panicReceiptSource struct{}

func (panicReceiptSource) Begin()                          { panic("begin") }
func (panicReceiptSource) Finish() *bursar.ProviderReceipt { panic("finish") }

func TestOptionsRejectInvalidConfiguration(t *testing.T) {
	valid := func() Options { return testOptions(&fakeCredits{}, nil) }
	tests := map[string]func(*Options){
		"missing credits":        func(options *Options) { options.Credits = nil },
		"unsupported credits":    func(options *Options) { options.Credits = struct{}{} },
		"missing measures":       func(options *Options) { options.Estimate.Measures = nil },
		"missing operation":      func(options *Options) { options.Estimate.Operation = " " },
		"negative ttl":           func(options *Options) { options.TTL = -time.Second },
		"blank reference type":   func(options *Options) { options.ReferenceType = " " },
		"blank operation prefix": func(options *Options) { options.OperationKeyPrefix = " " },
		"blank state namespace":  func(options *Options) { options.StateNamespace = " " },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			options := valid()
			mutate(&options)
			if _, err := New(options); err == nil {
				t.Fatal("expected configuration error")
			}
		})
	}

	credits := &fakeCredits{}
	options := valid()
	options.Credits = nil
	if plugin, err := NewPlugin(credits, options); err != nil || plugin == nil {
		t.Fatalf("NewPlugin = (%v, %v)", plugin, err)
	}
	if plugin, err := NewWithCredits(credits, options); err != nil || plugin == nil {
		t.Fatalf("NewWithCredits = (%v, %v)", plugin, err)
	}
}

func TestPluginDefaultsOperationKeyAndStateNamespace(t *testing.T) {
	t.Parallel()
	credits := &fakeCredits{}
	options := testOptions(credits, nil)
	options.OperationKeyPrefix = ""
	options.StateNamespace = ""
	p, err := New(options)
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-defaults")
	if response, err := p.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "model"}); err != nil || response != nil {
		t.Fatalf("before model = (%v, %v)", response, err)
	}
	if !strings.HasPrefix(credits.lastOptions.OperationKey, "adk-model:invocation-defaults:") {
		t.Fatalf("default operation key = %q", credits.lastOptions.OperationKey)
	}
	if _, err := state.Get("_bursar_model_leases:default:invocation-defaults"); err != nil {
		t.Fatalf("default state namespace was not used: %v", err)
	}
	if err := p.Close(); err != nil {
		t.Fatalf("close plugin: %v", err)
	}
}

func TestPluginSkipsUnbilledRequestsAndDeniesUnsafeCallbacks(t *testing.T) {
	credits := &fakeCredits{}
	options := testOptions(credits, panicReceiptSource{})
	options.ShouldBill = func(agent.Context, *model.LLMRequest) bool { return false }
	plugin, err := New(options)
	if err != nil {
		t.Fatal(err)
	}
	ctx := newFakeContext(newFakeState(), "invocation-1")
	if response, err := plugin.BeforeModelCallback()(ctx, nil); err != nil || response != nil {
		t.Fatalf("unbilled request = (%v, %v)", response, err)
	}
	if credits.operation != nil {
		t.Fatal("unbilled request reserved credits")
	}

	options.ShouldBill = func(agent.Context, *model.LLMRequest) bool { panic("selector") }
	options.AdmissionMessage = func(error) string { panic("message") }
	plugin, err = New(options)
	if err != nil {
		t.Fatal(err)
	}
	response, err := plugin.BeforeModelCallback()(ctx, nil)
	if err != nil || response == nil || response.ErrorMessage != genericBillingFailure {
		t.Fatalf("unsafe selector denial = (%+v, %v)", response, err)
	}

	options = testOptions(credits, nil)
	options.MetadataFactory = func(agent.Context) bursar.CreditMetadata { panic("metadata") }
	plugin, err = New(options)
	if err != nil {
		t.Fatal(err)
	}
	response, err = plugin.BeforeModelCallback()(ctx, nil)
	if err != nil || response == nil || credits.lastOptions.OperationKey != "" {
		t.Fatalf("metadata panic denial = (%+v, %v), options %+v", response, err, credits.lastOptions)
	}

	empty := &emptyInvocationContext{StrictContextMock: agent.NewStrictContextMock(context.Background())}
	options = testOptions(&fakeCredits{}, nil)
	plugin, err = New(options)
	if err != nil {
		t.Fatal(err)
	}
	if response, err = plugin.BeforeModelCallback()(empty, nil); err != nil || response == nil {
		t.Fatalf("missing context denial = (%+v, %v)", response, err)
	}
}

func TestPluginUsesResponseUsageAndRecoversDurableEntriesAfterRun(t *testing.T) {
	credits := &fakeCredits{}
	options := testOptions(credits, panicReceiptSource{})
	options.Provider = "google"
	options.Estimate.Dimensions["provider"] = ""
	options.Estimate.Dimensions["model"] = ""
	options.OperationType = "model_call"
	options.Feature = "tutor"
	options.TTL = time.Minute
	options.Estimate.Measures["cache_read_tokens"] = bursar.DecimalZero
	options.Estimate.Measures["reasoning_tokens"] = bursar.DecimalZero
	options.Estimate.Measures["web_search_calls"] = bursar.DecimalZero
	options.Estimate.Measures["code_exec_calls"] = bursar.DecimalZero
	options.MetadataFactory = func(agent.Context) bursar.CreditMetadata {
		return bursar.CreditMetadata{"reference_type": "lesson", "lesson_id": "lesson-1"}
	}
	plugin, err := New(options)
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-1")
	if response, err := plugin.BeforeModelCallback()(ctx, &model.LLMRequest{Model: "gemini-2.5"}); err != nil || response != nil {
		t.Fatalf("before model = (%v, %v)", response, err)
	}
	if credits.lastUserID != "user-1" || credits.lastOptions.OperationType != "model_call" || credits.lastOptions.Feature != "tutor" {
		t.Fatalf("begin options = user %q, %+v", credits.lastUserID, credits.lastOptions)
	}
	if credits.lastOptions.Metadata["reference_type"] != "lesson" || credits.lastOptions.Metadata["reference_id"] != "invocation-1" {
		t.Fatalf("begin metadata = %+v", credits.lastOptions.Metadata)
	}
	response := &model.LLMResponse{
		ModelVersion: "gemini-response",
		UsageMetadata: &genai.GenerateContentResponseUsageMetadata{
			PromptTokenCount: 3, CandidatesTokenCount: 2, TotalTokenCount: 0,
			CachedContentTokenCount: -1, ThoughtsTokenCount: 1,
		},
		Content: &genai.Content{Parts: []*genai.Part{
			{FunctionCall: &genai.FunctionCall{Name: "web_search"}},
			{FunctionCall: &genai.FunctionCall{Name: "code_exec"}},
			nil,
		}},
	}
	if returned, err := plugin.AfterModelCallback()(ctx, response, nil); err != nil || returned != nil {
		t.Fatalf("after model = (%v, %v)", returned, err)
	}
	if len(credits.operation.settled) != 1 {
		t.Fatalf("settlements = %d, want 1", len(credits.operation.settled))
	}
	metrics := credits.operation.settled[0]
	for key, want := range map[string]string{
		"input_tokens": "3", "output_tokens": "2", "total_tokens": "5", "cache_read_tokens": "0",
		"reasoning_tokens": "1", "tool_calls": "2", "web_search_calls": "1", "code_exec_calls": "1",
	} {
		if got := metrics.Measures[key]; !got.Equal(bursar.MustAmount(want)) {
			t.Errorf("%s = %s, want %s", key, got, want)
		}
	}
	if metrics.Dimensions["provider"] != "google" || metrics.Dimensions["model"] != "gemini-response" {
		t.Fatalf("dimensions = %+v", metrics.Dimensions)
	}

	key := "_bursar_model_leases:test:invocation-1"
	ready := encodeMetrics(bursar.UsageMetrics{Operation: "completion", Measures: map[string]bursar.Amount{"calls": bursar.MustAmount("1")}})
	if err := state.Set(key, []map[string]any{
		{"lease_id": "ready", "operation_key": "ready-op", "metrics": ready},
		{"lease_id": "invalid", "operation_key": "invalid-op", "metrics": map[string]any{"operation": "completion", "measures": map[string]any{"calls": "invalid"}}},
		{"lease_id": "unpriced", "operation_key": "unpriced-op"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := state.Set("unrelated", "value"); err != nil {
		t.Fatal(err)
	}
	plugin.AfterRunCallback()(ctx)
	value, err := state.Get(key)
	if err != nil || len(value.([]map[string]any)) != 0 {
		t.Fatalf("durable entries after recovery = %#v, %v", value, err)
	}
	if credits.resumes != 2 || credits.releases != 2 || len(credits.operation.settled) != 2 {
		t.Fatalf("recovery = resumes %d releases %d settlements %d", credits.resumes, credits.releases, len(credits.operation.settled))
	}
}

func TestPluginKeepsRetryableRecoveryAndDropsTerminalSettlement(t *testing.T) {
	credits := &fakeCredits{operation: &fakeOperation{leaseID: "lease-1"}}
	options := testOptions(credits, nil)
	plugin, err := New(options)
	if err != nil {
		t.Fatal(err)
	}
	state := newFakeState()
	ctx := newFakeContext(state, "invocation-1")
	key := "_bursar_model_leases:test:invocation-1"
	metrics := encodeMetrics(bursar.UsageMetrics{Operation: "completion", Measures: map[string]bursar.Amount{"calls": bursar.MustAmount("1")}})
	if err := state.Set(key, []map[string]any{{"lease_id": "lease-1", "operation_key": "op-1", "metrics": metrics}}); err != nil {
		t.Fatal(err)
	}
	credits.resumeErr = errors.New("temporary database failure")
	if _, err := plugin.BeforeRunCallback()(ctx); err != nil {
		t.Fatal(err)
	}
	value, _ := state.Get(key)
	if len(value.([]map[string]any)) != 1 {
		t.Fatal("retryable recovery error discarded the durable lease")
	}

	credits.resumeErr = bursar.NewError("lease already settled", bursar.ErrorOptions{Code: bursar.ErrorCodeLeaseNotFound, Retryable: false})
	if _, err := plugin.BeforeRunCallback()(ctx); err != nil {
		t.Fatal(err)
	}
	value, _ = state.Get(key)
	if len(value.([]map[string]any)) != 0 {
		t.Fatal("terminal settlement outcome retained the durable lease")
	}
}

func TestSmallHelpersCoverDefensiveBoundaries(t *testing.T) {
	logger := noopLogger{}
	logger.Debug("debug", nil)
	logger.Info("info", nil)
	logger.Warn("warn", nil)
	logger.Error("error", nil)

	if defaultSubjectResolver(nil) != "" || subjectOf(nil, nil) != "" || stateOf(nil) != nil || invocationID(nil) != "" {
		t.Fatal("nil ADK context helpers were not empty")
	}
	if defaultAdmissionMessage(nil) != genericBillingFailure || defaultAdmissionMessage(errors.New("private")) == "" {
		t.Fatal("admission error did not produce a safe public message")
	}
	if requestModel(nil) != "" || requestModel(&model.LLMRequest{Model: "model"}) != "model" {
		t.Fatal("request model helper mismatch")
	}
	if operationType(Options{OperationType: "override"}, bursar.UsageMetrics{Operation: "estimate"}) != "override" ||
		operationType(Options{}, bursar.UsageMetrics{Operation: "estimate"}) != "estimate" {
		t.Fatal("operation type helper mismatch")
	}
	if !nonNegative("bad").IsZero() || !nonNegative(-1).IsZero() || !nonNegative(3).Equal(bursar.MustAmount("3")) {
		t.Fatal("non-negative amount normalization mismatch")
	}
	if result, err := safeShouldBill(func(agent.Context, *model.LLMRequest) bool { return true }, nil, nil); err != nil || !result {
		t.Fatalf("safe selector = (%v, %v)", result, err)
	}
	if result, err := safeShouldBill(func(agent.Context, *model.LLMRequest) bool { panic("selector") }, nil, nil); err == nil || result {
		t.Fatalf("panic selector = (%v, %v)", result, err)
	}
	if !terminalSettlementError(nil) || terminalSettlementError(errors.New("temporary")) {
		t.Fatal("terminal settlement classification mismatch")
	}
	terminal := bursar.NewError("terminal", bursar.ErrorOptions{Code: bursar.ErrorCodeLeaseNotFound, Retryable: false})
	if !terminalSettlementError(terminal) {
		t.Fatal("non-retryable Bursar error was not terminal")
	}

	total, search, code := toolMeasures(nil)
	if !total.IsZero() || !search.IsZero() || !code.IsZero() {
		t.Fatal("nil response reported tool calls")
	}
	response := &model.LLMResponse{Content: &genai.Content{Parts: []*genai.Part{
		{FunctionCall: &genai.FunctionCall{Name: "search_web"}},
		{FunctionCall: &genai.FunctionCall{Name: "sandbox_exec"}},
		{FunctionCall: &genai.FunctionCall{}}, nil,
	}}}
	total, search, code = toolMeasures(response)
	if !total.Equal(bursar.MustAmount("2")) || !search.Equal(bursar.MustAmount("1")) || !code.Equal(bursar.MustAmount("1")) {
		t.Fatalf("tool measures = %s/%s/%s", total, search, code)
	}

	base := bursar.CreditMetadata{"reference_type": "base", "reference_id": "base-id"}
	receipt := &bursar.ProviderReceipt{Metrics: bursar.UsageMetrics{Operation: "completion", Measures: map[string]bursar.Amount{"calls": bursar.MustAmount("1")}}, Metadata: bursar.CreditMetadata{"reference_type": "unsafe", "reference_id": "unsafe", "provider_request_id": "request-1"}}
	merged := settlementMetadata(base, receipt)
	if merged["reference_type"] != "base" || merged["reference_id"] != "base-id" || merged["provider_request_id"] != "request-1" {
		t.Fatalf("settlement metadata = %+v", merged)
	}

	if _, err := decodeMetrics(map[string]any{"operation": "completion", "measures": map[string]any{"calls": "invalid"}}); err == nil {
		t.Fatal("invalid persisted metrics were accepted")
	}
	entries := []leaseEntry{{OperationKey: "first"}, {OperationKey: "second", Metrics: &bursar.UsageMetrics{}}}
	if activeEntryIndex(entries, "first") != 0 || activeEntryIndex(entries, "") != 0 || activeEntryIndex(entries, "missing") != -1 || activeEntryIndex(nil, "") != -1 {
		t.Fatal("active entry selection mismatch")
	}
	if activeEntryIndex([]leaseEntry{{OperationKey: "first"}, {OperationKey: "second"}}, "") != -1 {
		t.Fatal("ambiguous active entry selection did not fail closed")
	}
}

func TestLeaseStateDecoderAcceptsCanonicalFormsAndRejectsMalformedState(t *testing.T) {
	t.Parallel()
	adapter := &adapter{}
	state := newFakeState()
	const key = "lease-state"

	if err := state.Set(key, nil); err != nil {
		t.Fatal(err)
	}
	if entries, err := adapter.loadEntries(state, key); err != nil || entries != nil {
		t.Fatalf("nil lease state = %+v, error = %v", entries, err)
	}

	typed := []leaseEntry{{LeaseID: "lease-1", OperationKey: "operation-1", Metadata: bursar.CreditMetadata{"source": "typed"}}}
	if err := state.Set(key, typed); err != nil {
		t.Fatal(err)
	}
	if entries, err := adapter.loadEntries(state, key); err != nil || len(entries) != 1 || entries[0].OperationKey != "operation-1" {
		t.Fatalf("typed lease state = %+v, error = %v", entries, err)
	}

	validMapEntries := []any{
		map[string]any{"lease_id": "lease-map", "operation_key": "operation-map", "metadata": map[string]any{"source": "map"}},
		map[string]any{"lease_id": "lease-credit-metadata", "operation_key": "operation-credit-metadata", "metadata": bursar.CreditMetadata{"source": "typed-map"}},
		map[string]any{"lease_id": "lease-invalid-metrics", "operation_key": "operation-invalid-metrics", "metrics": "not-an-object"},
	}
	if err := state.Set(key, validMapEntries); err != nil {
		t.Fatal(err)
	}
	entries, err := adapter.loadEntries(state, key)
	if err != nil || len(entries) != 3 || entries[0].Metadata["source"] != "map" || entries[1].Metadata["source"] != "typed-map" || !entries[2].Invalid {
		t.Fatalf("canonical map lease state = %+v, error = %v", entries, err)
	}

	for _, test := range []struct {
		name  string
		value any
	}{
		{"typed entry without identity", []leaseEntry{{LeaseID: "lease-only"}}},
		{"unsupported container", "not-a-list"},
		{"unsupported entry", []any{17}},
		{"map entry without identity", []any{map[string]any{"lease_id": "lease-only"}}},
		{"invalid metadata", []any{map[string]any{"lease_id": "lease-1", "operation_key": "operation-1", "metadata": 17}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := state.Set(key, test.value); err != nil {
				t.Fatal(err)
			}
			if entries, err := adapter.loadEntries(state, key); err == nil || entries != nil {
				t.Fatalf("malformed lease state = %+v, error = %v", entries, err)
			}
		})
	}
}
