package bursar

import (
	"context"
	"testing"
)

type projectionRepositoryStub struct {
	usage         *UsageChargeExport
	billing       *BillingEventPayloadExport
	archivedID    string
	archivedKey   string
	archivedVer   *string
	archiveResult bool
}

func (r *projectionRepositoryStub) GetUsageCharge(context.Context, string) (*UsageChargeExport, error) {
	return r.usage, nil
}

func (r *projectionRepositoryStub) GetBillingEventPayload(context.Context, string) (*BillingEventPayloadExport, error) {
	return r.billing, nil
}

func (r *projectionRepositoryStub) ArchiveBillingEventPayload(_ context.Context, eventID, key string, version *string) (bool, error) {
	r.archivedID, r.archivedKey, r.archivedVer = eventID, key, version
	return r.archiveResult, nil
}

type usageSinkStub struct {
	event         UsageChargeExport
	outboxEventID string
}

func (s *usageSinkStub) WriteUsage(_ context.Context, event UsageChargeExport, outboxEventID string) error {
	s.event, s.outboxEventID = event, outboxEventID
	return nil
}

type billingArchiveStub struct {
	event  BillingEventPayloadExport
	result BillingPayloadArchiveResult
}

func (a *billingArchiveStub) Archive(_ context.Context, event BillingEventPayloadExport) (BillingPayloadArchiveResult, error) {
	a.event = event
	return a.result, nil
}

func TestUsageChargeOutboxHandlerUsesAuthoritativeFallback(t *testing.T) {
	repository := &projectionRepositoryStub{usage: &UsageChargeExport{
		TenantID: storageTestTenant, ChargeID: storageTestCharge, Requested: MustAmount("1.000000"), Charged: MustAmount("1.000000"),
	}}
	sink := &usageSinkStub{}
	handler, err := NewUsageChargeOutboxHandler(repository, sink)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{
		EventID: "10", TenantID: storageTestTenant, Topic: OutboxTopicUsageChargeRecorded,
		AggregateID: storageTestCharge, PayloadVersion: 1, Payload: map[string]any{},
	}
	if err := handler.Handle(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if sink.outboxEventID != "10" || sink.event.ChargeID != storageTestCharge {
		t.Fatalf("sink = %#v / %q", sink.event, sink.outboxEventID)
	}
	repository.usage.TenantID = storageOtherTenant
	if err := handler.Handle(context.Background(), event); err == nil {
		t.Fatal("cross-tenant usage export was accepted")
	}
}

func TestBillingPayloadOutboxHandlerArchivesAndRecordsPointer(t *testing.T) {
	payload := &BillingEventPayloadExport{TenantID: storageTestTenant, EventID: storageTestBilling, Provider: "stripe"}
	repository := &projectionRepositoryStub{billing: payload, archiveResult: true}
	version := "version-1"
	archive := &billingArchiveStub{result: BillingPayloadArchiveResult{Key: "tenant/event.json", VersionID: &version}}
	handler, err := NewBillingPayloadOutboxHandler(repository, archive)
	if err != nil {
		t.Fatal(err)
	}
	event := OutboxEvent{
		EventID: "11", TenantID: storageTestTenant, Topic: OutboxTopicBillingWebhookCompleted,
		AggregateID: storageTestBilling, PayloadVersion: 1, Payload: map[string]any{},
	}
	if err := handler.Handle(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if repository.archivedID != storageTestBilling || repository.archivedKey != "tenant/event.json" || repository.archivedVer == nil || *repository.archivedVer != version {
		t.Fatalf("archive pointer = %q %q %#v", repository.archivedID, repository.archivedKey, repository.archivedVer)
	}
	existingKey := "already-archived.json"
	repository.billing.ObjectKey = &existingKey
	repository.archivedID = ""
	if err := handler.Handle(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if repository.archivedID != "" {
		t.Fatal("already archived event was archived again")
	}
}
