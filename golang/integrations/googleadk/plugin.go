// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package googleadk bridges Google ADK v2 model callbacks to Bursar's
// metric-priced durable lease lifecycle. ADK remains an optional integration:
// importing the core SDK does not pull in ADK or its Go-version requirement.
package googleadk

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/Zonastery/bursar/golang/v2"
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model"
	"google.golang.org/adk/v2/plugin"
	"google.golang.org/adk/v2/session"
	"google.golang.org/genai"
)

const genericBillingFailure = "Billing service is temporarily unavailable. Please try again."

// Logger is the deliberately small logging port used by the adapter. The
// integration never logs prompts, provider payloads, or receipt secrets.
type Logger interface {
	Debug(string, map[string]any)
	Info(string, map[string]any)
	Warn(string, map[string]any)
	Error(string, map[string]any)
}

// BursarPlugin names the official ADK plugin type returned by New.
type BursarPlugin = plugin.Plugin

// Config is an ergonomic alias for Options.
type Config = Options

type noopLogger struct{}

func (noopLogger) Debug(string, map[string]any) {}
func (noopLogger) Info(string, map[string]any)  {}
func (noopLogger) Warn(string, map[string]any)  {}
func (noopLogger) Error(string, map[string]any) {}

// Operation is the narrow durable-operation surface needed by this adapter.
// The root CreditsService is accepted through RootCreditsClient below.
type Operation interface {
	LeaseID() string
	SettleUsage(context.Context, bursar.UsageMetrics) (bursar.DeductionResult, error)
	Release(context.Context) (bursar.ReleaseResult, error)
}

// CreditClient is the testable, framework-neutral lease port for the adapter.
// Implementations must preserve Bursar's idempotent durable semantics.
type CreditClient interface {
	BeginBilledUsageOperation(context.Context, string, bursar.BeginBilledUsageOperationOptions) (Operation, error)
	ResumeBilledOperation(string, string, string, string, bursar.CreditMetadata) (Operation, error)
	Release(context.Context, string, string) (bursar.ReleaseResult, error)
}

type rootCreditsClient struct{ service *bursar.CreditsService }

func (c rootCreditsClient) BeginBilledUsageOperation(ctx context.Context, userID string, options bursar.BeginBilledUsageOperationOptions) (Operation, error) {
	operation, err := c.service.BeginBilledUsageOperation(ctx, userID, options)
	if err != nil {
		return nil, err
	}
	return operation, nil
}

func (c rootCreditsClient) ResumeBilledOperation(userID, leaseID, operationKey, feature string, metadata bursar.CreditMetadata) (Operation, error) {
	return c.service.ResumeBilledOperation(userID, leaseID, operationKey, feature, metadata)
}

func (c rootCreditsClient) Release(ctx context.Context, userID, leaseID string) (bursar.ReleaseResult, error) {
	return c.service.Release(ctx, userID, leaseID)
}

// Options configures the ADK integration. Credits may be a CreditClient or a
// *bursar.CreditsService; the latter is wrapped automatically.
type Options struct {
	Credits            any
	Estimate           bursar.UsageMetrics
	OperationType      string
	BillingMode        bursar.BillingMode
	Feature            string
	Provider           string
	TTL                time.Duration
	ReceiptSource      bursar.ProviderReceiptSource
	SubjectResolver    func(agent.InvocationContext) string
	ShouldBill         func(agent.Context, *model.LLMRequest) bool
	AdmissionMessage   func(error) string
	MetadataFactory    func(agent.Context) bursar.CreditMetadata
	ReferenceType      string
	OperationKeyPrefix string
	StateNamespace     string
	Retry              bursar.BursarRetryOptions
	Logger             Logger
	Name               string
}

// New creates the official ADK plugin.Plugin using plugin.New/plugin.Config.
func New(options Options) (*plugin.Plugin, error) {
	client, err := normalizeCredits(options.Credits)
	if err != nil {
		return nil, err
	}
	if err := validateOptions(&options); err != nil {
		return nil, err
	}
	if options.Logger == nil {
		options.Logger = noopLogger{}
	}
	if options.SubjectResolver == nil {
		options.SubjectResolver = defaultSubjectResolver
	}
	if options.ShouldBill == nil {
		options.ShouldBill = func(agent.Context, *model.LLMRequest) bool { return true }
	}
	if options.AdmissionMessage == nil {
		options.AdmissionMessage = defaultAdmissionMessage
	}
	if options.ReferenceType == "" {
		options.ReferenceType = "adk_invocation"
	}
	if options.OperationKeyPrefix == "" {
		options.OperationKeyPrefix = "adk-model"
	}
	if options.StateNamespace == "" {
		options.StateNamespace = "default"
	}
	if options.Name == "" {
		options.Name = "bursar"
	}
	if options.Retry.MaxAttempts == 0 {
		options.Retry = bursar.DefaultBursarRetryOptions()
	}

	a := &adapter{client: client, options: options, statePrefix: "_bursar_model_leases:" + options.StateNamespace + ":", active: make(map[activeKey]string)}
	return plugin.New(plugin.Config{
		Name:                 options.Name,
		BeforeRunCallback:    a.beforeRun,
		AfterRunCallback:     a.afterRun,
		BeforeModelCallback:  a.beforeModel,
		AfterModelCallback:   a.afterModel,
		OnModelErrorCallback: a.onModelError,
		CloseFunc:            func() error { return nil },
	})
}

// NewPlugin is a convenience form matching the common Bursar constructor
// shape. It accepts either a CreditClient or *bursar.CreditsService.
func NewPlugin(credits any, options Options) (*plugin.Plugin, error) {
	options.Credits = credits
	return New(options)
}

// NewWithCredits is an explicit alias useful when constructing integrations
// from dependency-injection code.
func NewWithCredits(credits any, options Options) (*plugin.Plugin, error) {
	return NewPlugin(credits, options)
}

func normalizeCredits(value any) (CreditClient, error) {
	switch credits := value.(type) {
	case CreditClient:
		if credits == nil {
			break
		}
		return credits, nil
	case *bursar.CreditsService:
		if credits != nil {
			return rootCreditsClient{service: credits}, nil
		}
	}
	return nil, errors.New("googleadk: credits client is required")
}

func validateOptions(options *Options) error {
	if err := options.Estimate.Validate(); err != nil {
		return err
	}
	if len(options.Estimate.Measures) == 0 {
		return errors.New("googleadk: estimate must declare at least one billing measure")
	}
	if strings.TrimSpace(options.Estimate.Operation) == "" {
		return errors.New("googleadk: estimate operation must not be empty")
	}
	if options.TTL < 0 {
		return errors.New("googleadk: TTL must not be negative")
	}
	if options.ReferenceType != "" && strings.TrimSpace(options.ReferenceType) == "" {
		return errors.New("googleadk: reference type must not be empty")
	}
	if options.OperationKeyPrefix != "" && strings.TrimSpace(options.OperationKeyPrefix) == "" {
		return errors.New("googleadk: operation key prefix must not be empty")
	}
	if options.StateNamespace != "" && strings.TrimSpace(options.StateNamespace) == "" {
		return errors.New("googleadk: state namespace must not be empty")
	}
	return nil
}

func defaultSubjectResolver(ctx agent.InvocationContext) string {
	if ctx == nil || ctx.Session() == nil {
		return ""
	}
	return strings.TrimSpace(ctx.Session().UserID())
}

func defaultAdmissionMessage(err error) string {
	if err == nil {
		return genericBillingFailure
	}
	return bursar.PublicErrorMessage(err)
}

type activeKey struct {
	InvocationID string
	ContextID    string
}

type adapter struct {
	client      CreditClient
	options     Options
	statePrefix string
	mu          sync.Mutex
	active      map[activeKey]string
}

func (a *adapter) beforeRun(ctx agent.InvocationContext) (*genai.Content, error) {
	userID, state := a.invocationScope(ctx)
	if userID != "" && state != nil {
		a.settleReady(ctx, state, userID)
	}
	return nil, nil
}

func (a *adapter) beforeModel(ctx agent.Context, request *model.LLMRequest) (*model.LLMResponse, error) {
	shouldBill, selectorErr := safeShouldBill(a.options.ShouldBill, ctx, request)
	if selectorErr != nil {
		a.options.Logger.Error("adk_billing_selector_failed", map[string]any{"error_type": fmt.Sprintf("%T", selectorErr)})
		return a.admissionDenied(selectorErr)
	}
	if !shouldBill {
		return nil, nil
	}
	userID, state := a.invocationScope(ctx)
	invocationID := invocationID(ctx)
	if userID == "" || state == nil || invocationID == "" || ctx.Session() == nil {
		a.options.Logger.Error("adk_billing_context_missing", map[string]any{"has_user": userID != "", "has_state": state != nil, "has_invocation": invocationID != ""})
		return a.admissionDenied(nil)
	}
	if receipt := a.finishReceipt(); receipt != nil {
		a.complete(ctx, userID, state, invocationID, receipt, nil, "")
	}
	a.settleReady(ctx, state, userID)
	a.releaseUnpriced(state, userID, invocationID)
	operationKey := a.options.OperationKeyPrefix + ":" + invocationID + ":" + randomID()
	estimate := a.estimateForModel(request)
	metadata, metadataErr := a.baseMetadata(ctx, invocationID)
	if metadataErr != nil {
		return a.admissionDenied(metadataErr)
	}
	operation, err := retryBegin(ctx, a.client, userID, bursar.BeginBilledUsageOperationOptions{Estimate: estimate, OperationKey: operationKey, OperationType: operationType(a.options, estimate), BillingMode: a.options.BillingMode, TTL: a.options.TTL, Feature: a.options.Feature, Metadata: metadata}, a.options.Retry)
	if err != nil {
		a.options.Logger.Warn("adk_billing_reserve_failed", map[string]any{"retryable": bursar.IsRetryableError(err)})
		return a.admissionDenied(err)
	}
	entry := leaseEntry{LeaseID: operation.LeaseID(), OperationKey: operationKey, Metadata: metadata}
	if err := a.appendEntry(state, a.leaseKey(invocationID), entry); err != nil {
		_, _ = operation.Release(ctx)
		return a.admissionDenied(err)
	}
	a.mu.Lock()
	a.active[activeKey{InvocationID: invocationID, ContextID: contextID(ctx)}] = operationKey
	a.mu.Unlock()
	a.beginReceipt()
	return nil, nil
}

func (a *adapter) afterModel(ctx agent.Context, response *model.LLMResponse, responseErr error) (*model.LLMResponse, error) {
	if response != nil && response.Partial {
		return nil, responseErr
	}
	receipt := a.finishReceipt()
	if receipt != nil {
		a.complete(ctx, subjectOf(a.options.SubjectResolver, ctx), stateOf(ctx), invocationID(ctx), receipt, response, "")
	} else if responseErr != nil {
		a.releaseActive(ctx)
	} else {
		a.complete(ctx, subjectOf(a.options.SubjectResolver, ctx), stateOf(ctx), invocationID(ctx), nil, response, "")
	}
	return nil, responseErr
}

func (a *adapter) onModelError(ctx agent.Context, request *model.LLMRequest, responseErr error) (*model.LLMResponse, error) {
	receipt := a.finishReceipt()
	if receipt != nil {
		a.complete(ctx, subjectOf(a.options.SubjectResolver, ctx), stateOf(ctx), invocationID(ctx), receipt, nil, requestModel(request))
	} else {
		a.releaseActive(ctx)
	}
	return nil, responseErr
}

func (a *adapter) afterRun(ctx agent.InvocationContext) {
	userID, state := a.invocationScope(ctx)
	if userID == "" || state == nil || invocationID(ctx) == "" {
		return
	}
	if receipt := a.finishReceipt(); receipt != nil {
		a.complete(ctx, userID, state, invocationID(ctx), receipt, nil, "")
	}
	a.settleReady(ctx, state, userID)
	a.releaseUnpriced(state, userID, invocationID(ctx))
	a.clearActive(invocationID(ctx), "")
}

func (a *adapter) admissionDenied(err error) (*model.LLMResponse, error) {
	message := genericBillingFailure
	func() {
		defer func() { _ = recover() }()
		if a.options.AdmissionMessage != nil {
			if candidate := a.options.AdmissionMessage(err); strings.TrimSpace(candidate) != "" {
				message = candidate
			}
		}
	}()
	return &model.LLMResponse{Content: &genai.Content{Role: "model", Parts: []*genai.Part{{Text: message}}}, ErrorCode: "ADMISSION_DENIED", ErrorMessage: message}, nil
}

func (a *adapter) complete(ctx agent.InvocationContext, userID string, state session.State, invocation string, receipt *bursar.ProviderReceipt, response *model.LLMResponse, requestModel string) {
	if userID == "" || state == nil || invocation == "" {
		return
	}
	key := a.leaseKey(invocation)
	entries := a.loadEntries(state, key)
	operationKey := a.activeOperation(invocation, contextID(ctx))
	index := activeEntryIndex(entries, operationKey)
	if index < 0 {
		return
	}
	metrics := a.actualMetrics(receipt, response, requestModel)
	entries[index].Metrics = &metrics
	entries[index].Metadata = settlementMetadata(entries[index].Metadata, receipt)
	if err := a.saveEntries(state, key, entries); err != nil {
		return
	}
	a.clearActive(invocation, contextID(ctx))
	a.settleReady(ctx, state, userID, key)
}

func (a *adapter) releaseActive(ctx agent.Context) {
	userID, state := a.invocationScope(ctx)
	invocation := invocationID(ctx)
	if userID == "" || state == nil || invocation == "" {
		return
	}
	key := a.leaseKey(invocation)
	entries := a.loadEntries(state, key)
	index := activeEntryIndex(entries, a.activeOperation(invocation, contextID(ctx)))
	if index < 0 {
		return
	}
	if a.releaseLease(ctx, userID, entries[index].LeaseID) {
		entries = append(entries[:index], entries[index+1:]...)
		a.saveEntries(state, key, entries)
	}
	a.clearActive(invocation, contextID(ctx))
}

func (a *adapter) settleReady(ctx agent.InvocationContext, state session.State, userID string, only ...string) {
	keys := only
	if len(keys) == 0 {
		keys = a.stateKeys(state)
	}
	for _, key := range keys {
		if !strings.HasPrefix(key, a.statePrefix) {
			continue
		}
		entries := a.loadEntries(state, key)
		for index := 0; index < len(entries); {
			entry := entries[index]
			if entry.Invalid {
				if a.releaseLease(ctx, userID, entry.LeaseID) {
					entries = append(entries[:index], entries[index+1:]...)
					continue
				}
				index++
				continue
			}
			if entry.Metrics == nil {
				index++
				continue
			}
			metrics := *entry.Metrics
			operation, err := a.client.ResumeBilledOperation(userID, entry.LeaseID, entry.OperationKey, a.options.Feature, entry.Metadata)
			if err == nil {
				_, err = retrySettle(ctx, operation, metrics, a.options.Retry)
			}
			if err == nil || terminalSettlementError(err) {
				entries = append(entries[:index], entries[index+1:]...)
				continue
			}
			index++
		}
		_ = a.saveEntries(state, key, entries)
	}
}

func (a *adapter) releaseUnpriced(state session.State, userID, invocation string) {
	key := a.leaseKey(invocation)
	entries := a.loadEntries(state, key)
	for index := len(entries) - 1; index >= 0; index-- {
		if entries[index].Metrics != nil || !a.releaseLease(context.Background(), userID, entries[index].LeaseID) {
			continue
		}
		entries = append(entries[:index], entries[index+1:]...)
	}
	_ = a.saveEntries(state, key, entries)
}

func (a *adapter) releaseLease(ctx context.Context, userID, leaseID string) bool {
	_, err := retryRelease(ctx, a.client, userID, leaseID, a.options.Retry)
	if err == nil {
		return true
	}
	bursarErr, ok := bursar.AsBursarError(err)
	return ok && !bursarErr.Retryable
}

func (a *adapter) actualMetrics(receipt *bursar.ProviderReceipt, response *model.LLMResponse, requestModel string) bursar.UsageMetrics {
	metrics := cloneMetrics(a.options.Estimate)
	allowedMeasure := func(key string) bool { _, ok := a.options.Estimate.Measures[key]; return ok }
	if response != nil && response.UsageMetadata != nil {
		usage := response.UsageMetadata
		inputTokens := nonNegative(usage.PromptTokenCount)
		outputTokens := nonNegative(usage.CandidatesTokenCount)
		totalTokens := nonNegative(usage.TotalTokenCount)
		if totalTokens.IsZero() && (!inputTokens.IsZero() || !outputTokens.IsZero()) {
			totalTokens = inputTokens.Add(outputTokens)
		}
		values := map[string]bursar.Amount{"calls": bursar.MustAmount("1"), "input_tokens": inputTokens, "output_tokens": outputTokens, "total_tokens": totalTokens, "cache_read_tokens": nonNegative(usage.CachedContentTokenCount), "reasoning_tokens": nonNegative(usage.ThoughtsTokenCount)}
		for key, value := range values {
			if allowedMeasure(key) {
				metrics.Measures[key] = value
			}
		}
	}
	if receipt != nil && receipt.Validate() == nil {
		for key, value := range receipt.Metrics.Measures {
			if allowedMeasure(key) {
				metrics.Measures[key] = value
			}
		}
		for key, value := range receipt.Metrics.Dimensions {
			if _, ok := a.options.Estimate.Dimensions[key]; ok {
				metrics.Dimensions[key] = value
			}
		}
	}
	toolCalls, webSearch, codeExec := toolMeasures(response)
	for key, value := range map[string]bursar.Amount{"tool_calls": toolCalls, "web_search_calls": webSearch, "code_exec_calls": codeExec} {
		if allowedMeasure(key) {
			metrics.Measures[key] = value
		}
	}
	if _, ok := a.options.Estimate.Dimensions["provider"]; ok && a.options.Provider != "" {
		if _, exists := metrics.Dimensions["provider"]; !exists || strings.TrimSpace(fmt.Sprint(metrics.Dimensions["provider"])) == "" {
			metrics.Dimensions["provider"] = a.options.Provider
		}
	}
	if _, ok := a.options.Estimate.Dimensions["model"]; ok {
		if _, exists := metrics.Dimensions["model"]; !exists || strings.TrimSpace(fmt.Sprint(metrics.Dimensions["model"])) == "" {
			if response != nil && response.ModelVersion != "" {
				metrics.Dimensions["model"] = response.ModelVersion
			} else if requestModel != "" {
				metrics.Dimensions["model"] = requestModel
			}
		}
	}
	return metrics
}

func (a *adapter) estimateForModel(request *model.LLMRequest) bursar.UsageMetrics {
	metrics := cloneMetrics(a.options.Estimate)
	if request != nil {
		if _, ok := metrics.Dimensions["model"]; ok && request.Model != "" {
			metrics.Dimensions["model"] = request.Model
		}
	}
	if _, ok := metrics.Dimensions["provider"]; ok && a.options.Provider != "" {
		metrics.Dimensions["provider"] = a.options.Provider
	}
	return metrics
}

func (a *adapter) baseMetadata(ctx agent.Context, invocation string) (metadata bursar.CreditMetadata, panicErr error) {
	metadata = bursar.CreditMetadata{}
	defer func() {
		if recovered := recover(); recovered != nil {
			panicErr = fmt.Errorf("billing metadata factory panicked: %v", recovered)
		}
	}()
	if a.options.MetadataFactory != nil {
		if custom := a.options.MetadataFactory(ctx); custom != nil {
			metadata = custom.Clone()
		}
	}
	if _, ok := metadata["reference_type"]; !ok {
		metadata["reference_type"] = a.options.ReferenceType
	}
	if _, ok := metadata["reference_id"]; !ok {
		metadata["reference_id"] = invocation
	}
	return metadata, nil
}

func (a *adapter) beginReceipt() {
	if a.options.ReceiptSource != nil {
		func() { defer func() { _ = recover() }(); a.options.ReceiptSource.Begin() }()
	}
}
func (a *adapter) finishReceipt() *bursar.ProviderReceipt {
	if a.options.ReceiptSource == nil {
		return nil
	}
	var receipt *bursar.ProviderReceipt
	func() {
		defer func() { _ = recover() }()
		if captured := a.options.ReceiptSource.Finish(); captured != nil {
			copy := captured.Clone()
			receipt = &copy
		}
	}()
	return receipt
}

func (a *adapter) invocationScope(ctx agent.InvocationContext) (string, session.State) {
	if ctx == nil || ctx.Session() == nil {
		return "", nil
	}
	return safeSubject(a.options.SubjectResolver, ctx), ctx.Session().State()
}
func subjectOf(resolve func(agent.InvocationContext) string, ctx agent.InvocationContext) string {
	if resolve == nil || ctx == nil || ctx.Session() == nil {
		return ""
	}
	return safeSubject(resolve, ctx)
}

func safeSubject(resolve func(agent.InvocationContext) string, ctx agent.InvocationContext) (subject string) {
	defer func() { _ = recover() }()
	if resolve == nil {
		return ""
	}
	return strings.TrimSpace(resolve(ctx))
}
func stateOf(ctx agent.InvocationContext) session.State {
	if ctx == nil || ctx.Session() == nil {
		return nil
	}
	return ctx.Session().State()
}
func invocationID(ctx agent.InvocationContext) string {
	if ctx == nil {
		return ""
	}
	return strings.TrimSpace(ctx.InvocationID())
}
func contextID(ctx agent.InvocationContext) string   { return fmt.Sprintf("%p", ctx) }
func (a *adapter) leaseKey(invocation string) string { return a.statePrefix + invocation }
func (a *adapter) activeOperation(invocation, context string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.active[activeKey{InvocationID: invocation, ContextID: context}]
}
func (a *adapter) clearActive(invocation, context string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if context != "" {
		delete(a.active, activeKey{InvocationID: invocation, ContextID: context})
		return
	}
	for key := range a.active {
		if key.InvocationID == invocation {
			delete(a.active, key)
		}
	}
}

type leaseEntry struct {
	LeaseID      string                `json:"lease_id"`
	OperationKey string                `json:"operation_key"`
	Metrics      *bursar.UsageMetrics  `json:"metrics,omitempty"`
	Metadata     bursar.CreditMetadata `json:"metadata,omitempty"`
	Invalid      bool
}

func (a *adapter) loadEntries(state session.State, key string) []leaseEntry {
	value, err := state.Get(key)
	if err != nil {
		return nil
	}
	raw, ok := value.([]any)
	if !ok {
		if typed, yes := value.([]map[string]any); yes {
			raw = make([]any, len(typed))
			for index := range typed {
				raw[index] = typed[index]
			}
		} else {
			if typed, yes := value.([]leaseEntry); yes {
				return typed
			}
		}
	}
	entries := make([]leaseEntry, 0, len(raw))
	for _, candidate := range raw {
		payload, ok := candidate.(map[string]any)
		if !ok {
			continue
		}
		entry := leaseEntry{}
		if value, ok := payload["lease_id"].(string); ok {
			entry.LeaseID = value
		}
		if value, ok := payload["operation_key"].(string); ok {
			entry.OperationKey = value
		}
		if entry.LeaseID == "" || entry.OperationKey == "" {
			continue
		}
		if metadata, ok := payload["metadata"].(map[string]any); ok {
			entry.Metadata = bursar.CreditMetadata(metadata)
		}
		if metrics, ok := payload["metrics"].(map[string]any); ok {
			if parsed, err := decodeMetrics(metrics); err == nil {
				entry.Metrics = &parsed
			} else {
				entry.Invalid = true
			}
		} else if _, exists := payload["metrics"]; exists {
			entry.Invalid = true
		}
		entries = append(entries, entry)
	}
	return entries
}
func (a *adapter) saveEntries(state session.State, key string, entries []leaseEntry) error {
	if len(entries) == 0 {
		return state.Set(key, []map[string]any{})
	}
	payload := make([]map[string]any, 0, len(entries))
	for _, entry := range entries {
		item := map[string]any{"lease_id": entry.LeaseID, "operation_key": entry.OperationKey}
		if entry.Metrics != nil {
			item["metrics"] = encodeMetrics(*entry.Metrics)
		}
		if entry.Metadata != nil {
			item["metadata"] = entry.Metadata
		}
		payload = append(payload, item)
	}
	return state.Set(key, payload)
}
func (a *adapter) appendEntry(state session.State, key string, entry leaseEntry) error {
	entries := a.loadEntries(state, key)
	entries = append(entries, entry)
	if err := a.saveEntries(state, key, entries); err != nil {
		return err
	}
	persisted := a.loadEntries(state, key)
	found := false
	for _, candidate := range persisted {
		if candidate.LeaseID == entry.LeaseID && candidate.OperationKey == entry.OperationKey {
			found = true
			break
		}
	}
	if !found {
		return errors.New("googleadk: failed to persist lease state")
	}
	return nil
}
func (a *adapter) stateKeys(state session.State) []string {
	keys := []string{}
	for key := range state.All() {
		if strings.HasPrefix(key, a.statePrefix) {
			keys = append(keys, key)
		}
	}
	return keys
}

func encodeMetrics(metrics bursar.UsageMetrics) map[string]any {
	measures := map[string]any{}
	for key, value := range metrics.Measures {
		measures[key] = value.String()
	}
	return map[string]any{"operation": metrics.Operation, "measures": measures, "dimensions": metrics.Dimensions}
}
func decodeMetrics(payload map[string]any) (bursar.UsageMetrics, error) {
	operation, _ := payload["operation"].(string)
	measures := map[string]bursar.Amount{}
	raw, _ := payload["measures"].(map[string]any)
	for key, value := range raw {
		amount, err := bursar.NewAmount(fmt.Sprint(value))
		if err != nil {
			return bursar.UsageMetrics{}, err
		}
		measures[key] = amount
	}
	dimensions, _ := payload["dimensions"].(map[string]any)
	metrics := bursar.UsageMetrics{Operation: operation, Measures: measures, Dimensions: dimensions}
	return metrics, metrics.Validate()
}

func cloneMetrics(metrics bursar.UsageMetrics) bursar.UsageMetrics {
	clone := metrics
	clone.Measures = map[string]bursar.Amount{}
	for key, value := range metrics.Measures {
		clone.Measures[key] = value
	}
	clone.Dimensions = map[string]any{}
	for key, value := range metrics.Dimensions {
		clone.Dimensions[key] = value
	}
	return clone
}
func settlementMetadata(base bursar.CreditMetadata, receipt *bursar.ProviderReceipt) bursar.CreditMetadata {
	result := base.Clone()
	if receipt == nil || receipt.Validate() != nil {
		return result
	}
	for key, value := range receipt.Metadata {
		if key != "reference_type" && key != "reference_id" {
			result[key] = value
		}
	}
	return result
}
func activeEntryIndex(entries []leaseEntry, operationKey string) int {
	if operationKey != "" {
		for index := range entries {
			if entries[index].OperationKey == operationKey {
				return index
			}
		}
	}
	for index := range entries {
		if entries[index].Metrics == nil {
			return index
		}
	}
	return -1
}
func requestModel(request *model.LLMRequest) string {
	if request == nil {
		return ""
	}
	return request.Model
}
func safeShouldBill(selector func(agent.Context, *model.LLMRequest) bool, ctx agent.Context, request *model.LLMRequest) (result bool, panicErr error) {
	result = true
	defer func() {
		if recovered := recover(); recovered != nil {
			panicErr = fmt.Errorf("billing selector panicked: %v", recovered)
			result = false
		}
	}()
	return selector(ctx, request), nil
}
func operationType(options Options, estimate bursar.UsageMetrics) string {
	if strings.TrimSpace(options.OperationType) != "" {
		return options.OperationType
	}
	return estimate.Operation
}
func randomID() string {
	var bytes [16]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(bytes[:])
}
func nonNegative(value any) bursar.Amount {
	text := fmt.Sprint(value)
	amount, err := bursar.NewAmount(text)
	if err != nil || amount.IsNegative() {
		return bursar.DecimalZero
	}
	return amount
}
func terminalSettlementError(err error) bool {
	if err == nil {
		return true
	}
	bursarErr, ok := bursar.AsBursarError(err)
	return ok && !bursarErr.Retryable
}
func toolMeasures(response *model.LLMResponse) (bursar.Amount, bursar.Amount, bursar.Amount) {
	if response == nil || response.Content == nil {
		return bursar.DecimalZero, bursar.DecimalZero, bursar.DecimalZero
	}
	var total, search, code int
	for _, part := range response.Content.Parts {
		if part == nil || part.FunctionCall == nil || part.FunctionCall.Name == "" {
			continue
		}
		total++
		name := strings.ToLower(part.FunctionCall.Name)
		if strings.Contains(name, "search") {
			search++
		}
		if strings.Contains(name, "code") || strings.Contains(name, "exec") || strings.Contains(name, "sandbox") {
			code++
		}
	}
	return bursar.MustAmount(fmt.Sprint(total)), bursar.MustAmount(fmt.Sprint(search)), bursar.MustAmount(fmt.Sprint(code))
}

func retryBegin(ctx context.Context, client CreditClient, userID string, options bursar.BeginBilledUsageOperationOptions, retry bursar.BursarRetryOptions) (Operation, error) {
	return bursar.RetryBursarOperation(ctx, func(ctx context.Context) (Operation, error) {
		return client.BeginBilledUsageOperation(ctx, userID, options)
	}, retry)
}
func retrySettle(ctx context.Context, operation Operation, metrics bursar.UsageMetrics, retry bursar.BursarRetryOptions) (bursar.DeductionResult, error) {
	return bursar.RetryBursarOperation(ctx, func(ctx context.Context) (bursar.DeductionResult, error) { return operation.SettleUsage(ctx, metrics) }, retry)
}
func retryRelease(ctx context.Context, client CreditClient, userID, leaseID string, retry bursar.BursarRetryOptions) (bursar.ReleaseResult, error) {
	return bursar.RetryBursarOperation(ctx, func(ctx context.Context) (bursar.ReleaseResult, error) { return client.Release(ctx, userID, leaseID) }, retry)
}

var _ llmagent.BeforeModelCallback = (*adapter)(nil).beforeModel
