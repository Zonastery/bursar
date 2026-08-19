// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package s3

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	awss3 "github.com/aws/aws-sdk-go-v2/service/s3"
)

func TestArchiveLifecycleAndValidationBranches(t *testing.T) {
	t.Parallel()
	if err := (*S3BillingArchive)(nil).Start(context.Background()); err != nil {
		t.Fatalf("nil Start() error = %v", err)
	}
	if err := (*S3BillingArchive)(nil).Flush(context.Background()); err != nil {
		t.Fatalf("nil Flush() error = %v", err)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	archive := &S3BillingArchive{}
	if err := archive.Start(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled Start() error = %v", err)
	}
	if err := archive.Flush(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled Flush() error = %v", err)
	}
	if _, err := archive.Archive(canceled, testBillingEvent()); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled Archive() error = %v", err)
	}
	if err := archive.Close(canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled Close() error = %v", err)
	}

	if _, err := NewBillingArchive(Options{Bucket: "archive", Client: &clientStub{}, ClientFactory: func(context.Context) (Client, error) { return nil, nil }}); err == nil {
		t.Fatal("client and factory validation error = nil")
	}
	blank := "   "
	for _, options := range []Options{
		{Bucket: "archive", PutObject: PutObjectOptions{SSEKMSKeyID: &blank}},
		{Bucket: "archive", PutObject: PutObjectOptions{ChecksumSHA256: &blank}},
		{Bucket: "archive", Credentials: &Credentials{}},
		{Bucket: "archive", Credentials: &Credentials{AccessKeyID: "key"}},
		{Bucket: "archive", Credentials: &Credentials{AccessKeyID: "key", SecretAccessKey: "secret", SessionToken: blank}},
	} {
		if _, err := NewBillingArchive(options); err == nil {
			t.Fatalf("invalid options %+v returned nil error", options)
		}
	}
	if _, err := NewBillingArchive(Options{Bucket: "archive", Region: " us-east-1 ", Endpoint: " https://s3.example ", Credentials: &Credentials{AccessKeyID: "key", SecretAccessKey: "secret", SessionToken: " token "}}); err != nil {
		t.Fatalf("valid lazy AWS options error = %v", err)
	}

	for _, test := range []struct {
		name   string
		mutate func(*bursarEventAlias)
	}{
		{name: "provider", mutate: func(event *bursarEventAlias) { event.Provider = " " }},
		{name: "environment", mutate: func(event *bursarEventAlias) { event.ProviderEnvironment = " " }},
		{name: "provider event", mutate: func(event *bursarEventAlias) { event.ProviderEventID = " " }},
		{name: "event type", mutate: func(event *bursarEventAlias) { event.EventType = " " }},
		{name: "received at", mutate: func(event *bursarEventAlias) { event.ReceivedAt = zeroTime() }},
		{name: "completed at", mutate: func(event *bursarEventAlias) { value := zeroTime(); event.CompletedAt = &value }},
	} {
		t.Run(test.name, func(t *testing.T) {
			client := &clientStub{result: &awss3.PutObjectOutput{}}
			archive, err := NewBillingArchive(Options{Bucket: "archive", Client: client})
			if err != nil {
				t.Fatal(err)
			}
			event := testBillingEvent()
			alias := bursarEventAlias{BillingEventPayloadExport: event}
			test.mutate(&alias)
			if _, err := archive.Archive(context.Background(), alias.BillingEventPayloadExport); err == nil {
				t.Fatal("Archive() error = nil")
			}
			if len(client.inputs) != 0 {
				t.Fatal("invalid event reached PutObject")
			}
		})
	}
}

// These aliases keep the table-driven mutation cases compact without changing
// the production API or introducing a second event fixture.
type bursarEventAlias struct {
	bursar.BillingEventPayloadExport
}

func zeroTime() time.Time { return time.Time{} }

func TestArchivePutObjectFailureAndCloseOwnership(t *testing.T) {
	t.Parallel()
	failing := &clientStub{err: errors.New("network down")}
	archive, err := NewBillingArchive(Options{Bucket: "archive", Client: failing})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := archive.Archive(context.Background(), testBillingEvent()); err == nil || !strings.Contains(err.Error(), "network down") {
		t.Fatalf("PutObject error = %v", err)
	}

	nilResult := &clientStub{}
	archive, err = NewBillingArchive(Options{Bucket: "archive", Client: nilResult})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := archive.Archive(context.Background(), testBillingEvent()); err == nil || !strings.Contains(err.Error(), "no result") {
		t.Fatalf("nil PutObject result error = %v", err)
	}
	if err := archive.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if nilResult.closeCount != 0 {
		t.Fatalf("default injected client close count = %d, want 0", nilResult.closeCount)
	}
	if err := archive.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

type noCloseClient struct{}

func (noCloseClient) PutObject(context.Context, *awss3.PutObjectInput, ...func(*awss3.Options)) (*awss3.PutObjectOutput, error) {
	return &awss3.PutObjectOutput{}, nil
}

func TestArchiveDefaultFactoryAndNonClosableClient(t *testing.T) {
	t.Parallel()
	archive, err := NewBillingArchive(Options{
		Bucket: "archive", Region: "us-east-1", Endpoint: "https://s3.example",
		ForcePathStyle: true, Credentials: &Credentials{AccessKeyID: "key", SecretAccessKey: "secret"},
		Prefix: func() *string { value := " /billing/ "; return &value }(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := archive.getClient(context.Background()); err != nil {
		t.Fatalf("default client factory error = %v", err)
	}
	if err := archive.Close(context.Background()); err != nil {
		t.Fatalf("default client Close() error = %v", err)
	}

	archive, err = NewBillingArchive(Options{Bucket: "archive", Client: noCloseClient{}, OwnsClient: func() *bool { value := true; return &value }()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := archive.getClient(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(context.Background()); err != nil {
		t.Fatalf("non-closable client Close() error = %v", err)
	}
}

func TestS3NormalizationHelpers(t *testing.T) {
	t.Parallel()
	if got, err := normalizeOptional("  us-east-1 ", "region"); err != nil || got != "us-east-1" {
		t.Fatalf("normalizeOptional() = %q, %v", got, err)
	}
	if got, err := normalizeOptional("", "region"); err != nil || got != "" {
		t.Fatalf("empty normalizeOptional() = %q, %v", got, err)
	}
	if _, err := normalizeOptionalPointer(nil, "checksum"); err != nil {
		t.Fatal(err)
	}
	value := " value "
	got, err := normalizeOptionalPointer(&value, "checksum")
	if err != nil || got == nil || *got != "value" {
		t.Fatalf("normalizeOptionalPointer() = %v, %v", got, err)
	}
	credentials, err := normalizeCredentials(&Credentials{AccessKeyID: " key ", SecretAccessKey: " secret ", SessionToken: " token "})
	if err != nil || credentials == nil || credentials.AccessKeyID != "key" || credentials.SecretAccessKey != "secret" || credentials.SessionToken != "token" {
		t.Fatalf("normalizeCredentials() = %+v, %v", credentials, err)
	}
	if value := boolOption(nil, true); !value {
		t.Fatal("nil bool option did not use fallback")
	}
	falseValue := false
	if value := boolOption(&falseValue, true); value {
		t.Fatal("explicit false bool option used fallback")
	}
}
