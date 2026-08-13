// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package s3

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"sync"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	awss3 "github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

const (
	testTenantID = "11111111-1111-4111-8111-111111111111"
	testEventID  = "22222222-2222-4222-8222-222222222222"
)

type clientStub struct {
	mu         sync.Mutex
	inputs     []*awss3.PutObjectInput
	result     *awss3.PutObjectOutput
	err        error
	closeCount int
}

func (s *clientStub) PutObject(_ context.Context, input *awss3.PutObjectInput, _ ...func(*awss3.Options)) (*awss3.PutObjectOutput, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.inputs = append(s.inputs, input)
	return s.result, s.err
}

func (s *clientStub) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closeCount++
	return nil
}

func TestArchiveWritesCanonicalEnvelope(t *testing.T) {
	t.Parallel()
	version := "v-1"
	kmsKey := "arn:aws:kms:us-east-1:123456789012:key/example"
	client := &clientStub{result: &awss3.PutObjectOutput{VersionId: &version}}
	archive, err := NewBillingArchive(Options{
		Bucket: " billing-archive ",
		Client: client,
		PutObject: PutObjectOptions{
			ServerSideEncryption: types.ServerSideEncryptionAwsKms,
			SSEKMSKeyID:          &kmsKey,
		},
	})
	if err != nil {
		t.Fatalf("NewBillingArchive() error = %v", err)
	}
	completedAt := time.Date(2026, 8, 13, 10, 0, 0, 123_000_000, time.FixedZone("IST", 5*60*60+30*60))
	event := bursar.BillingEventPayloadExport{
		TenantID: testTenantID, EventID: testEventID, Provider: "stripe",
		ProviderEnvironment: "live", ProviderEventID: "evt_123", EventType: "payment.succeeded",
		ReceivedAt:  time.Date(2026, 8, 12, 23, 45, 1, 2_000, time.FixedZone("IST", 5*60*60+30*60)),
		CompletedAt: &completedAt, Envelope: map[string]any{"id": "evt_123", "livemode": true},
	}

	result, err := archive.Archive(context.Background(), event)
	if err != nil {
		t.Fatalf("Archive() error = %v", err)
	}
	if result.Key != "bursar/tenants/11111111-1111-4111-8111-111111111111/billing-events/2026/08/12/22222222-2222-4222-8222-222222222222.json" {
		t.Fatalf("Archive() key = %q", result.Key)
	}
	if result.VersionID == nil || *result.VersionID != version {
		t.Fatalf("Archive() version = %#v", result.VersionID)
	}
	if len(client.inputs) != 1 {
		t.Fatalf("PutObject calls = %d", len(client.inputs))
	}
	input := client.inputs[0]
	if input.Bucket == nil || *input.Bucket != "billing-archive" || input.Key == nil || *input.Key != result.Key {
		t.Fatalf("PutObject target = bucket %#v key %#v", input.Bucket, input.Key)
	}
	if input.ContentType == nil || *input.ContentType != "application/json" {
		t.Fatalf("PutObject content type = %#v", input.ContentType)
	}
	if input.ServerSideEncryption != types.ServerSideEncryptionAwsKms || input.SSEKMSKeyId == nil || *input.SSEKMSKeyId != kmsKey {
		t.Fatalf("PutObject encryption = %q key %#v", input.ServerSideEncryption, input.SSEKMSKeyId)
	}
	if input.Metadata["bursar-tenant-id"] != testTenantID || input.Metadata["bursar-event-id"] != testEventID {
		t.Fatalf("PutObject metadata = %#v", input.Metadata)
	}
	body, err := io.ReadAll(input.Body)
	if err != nil {
		t.Fatalf("ReadAll(body) error = %v", err)
	}
	var document map[string]any
	if err := json.Unmarshal(body, &document); err != nil {
		t.Fatalf("Unmarshal(body) error = %v", err)
	}
	if document["schema"] != "bursar.billing-event-envelope.v1" || document["receivedAt"] != "2026-08-12T18:15:01.000002Z" {
		t.Fatalf("archive document = %#v", document)
	}
	if document["completedAt"] != "2026-08-13T04:30:00.123Z" {
		t.Fatalf("archive completedAt = %#v", document["completedAt"])
	}
}

func TestArchiveFactoryFailureIsRetryableAndConstructedOnce(t *testing.T) {
	t.Parallel()
	client := &clientStub{result: &awss3.PutObjectOutput{}}
	var mu sync.Mutex
	factoryCalls := 0
	archive, err := NewBillingArchive(Options{
		Bucket: "archive",
		ClientFactory: func(context.Context) (Client, error) {
			mu.Lock()
			defer mu.Unlock()
			factoryCalls++
			if factoryCalls == 1 {
				return nil, errors.New("temporary credential failure")
			}
			return client, nil
		},
	})
	if err != nil {
		t.Fatalf("NewBillingArchive() error = %v", err)
	}
	event := testBillingEvent()
	if _, err := archive.Archive(context.Background(), event); err == nil {
		t.Fatal("Archive() error = nil, want factory error")
	}
	if _, err := archive.Archive(context.Background(), event); err != nil {
		t.Fatalf("Archive() retry error = %v", err)
	}
	if _, err := archive.Archive(context.Background(), event); err != nil {
		t.Fatalf("Archive() cached client error = %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if factoryCalls != 2 {
		t.Fatalf("factory calls = %d, want 2", factoryCalls)
	}
}

func TestArchiveValidationAndOwnership(t *testing.T) {
	t.Parallel()
	if _, err := NewBillingArchive(Options{Bucket: " "}); err == nil {
		t.Fatal("NewBillingArchive(empty bucket) error = nil")
	}
	client := &clientStub{result: &awss3.PutObjectOutput{}}
	owns := true
	archive, err := NewBillingArchive(Options{Bucket: "archive", Client: client, OwnsClient: &owns})
	if err != nil {
		t.Fatalf("NewBillingArchive() error = %v", err)
	}
	event := testBillingEvent()
	event.Envelope = nil
	if _, err := archive.Archive(context.Background(), event); err == nil {
		t.Fatal("Archive(nil envelope) error = nil")
	}
	event = testBillingEvent()
	event.TenantID = "not-a-uuid"
	if _, err := archive.Archive(context.Background(), event); err == nil {
		t.Fatal("Archive(invalid tenant) error = nil")
	}
	if err := archive.Close(context.Background()); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if client.closeCount != 1 {
		t.Fatalf("Close() client close count = %d", client.closeCount)
	}
}

func testBillingEvent() bursar.BillingEventPayloadExport {
	return bursar.BillingEventPayloadExport{
		TenantID: testTenantID, EventID: testEventID, Provider: "stripe",
		ProviderEnvironment: "test", ProviderEventID: "evt_123", EventType: "customer.created",
		ReceivedAt: time.Date(2026, 8, 13, 1, 2, 3, 0, time.UTC), Envelope: map[string]any{"id": "evt_123"},
	}
}
