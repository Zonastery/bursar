// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

// Package s3 provides the optional S3-compatible billing payload archive.
// PostgreSQL remains the authoritative envelope store and records the object
// key only after this adapter confirms a successful PutObject call.
package s3

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	bursar "github.com/Zonastery/bursar/golang/v2"
	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	awss3 "github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/google/uuid"
)

// Client is the official AWS SDK for Go v2 PutObject surface used by the
// archive. *s3.Client satisfies this interface, while the narrow boundary also
// permits deterministic tests and preconfigured S3-compatible clients.
type Client interface {
	PutObject(context.Context, *awss3.PutObjectInput, ...func(*awss3.Options)) (*awss3.PutObjectOutput, error)
}

// ClientFactory lazily creates an archive client. A failed factory call is not
// cached, so a later outbox retry can recover after credentials or networking
// become available.
type ClientFactory func(context.Context) (Client, error)

// Credentials selects explicit static credentials. Omit it to use the AWS SDK
// credential provider chain.
type Credentials struct {
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
}

// PutObjectOptions contains the encryption and checksum controls that are safe
// to apply to every archived object. Bucket, key, body, content type, and Bursar
// metadata are always owned by the adapter.
type PutObjectOptions struct {
	ServerSideEncryption    types.ServerSideEncryption
	SSEKMSKeyID             *string
	SSEKMSEncryptionContext *string
	BucketKeyEnabled        *bool
	ChecksumAlgorithm       types.ChecksumAlgorithm
	ChecksumCRC32           *string
	ChecksumCRC32C          *string
	ChecksumCRC64NVME       *string
	ChecksumMD5             *string
	ChecksumSHA1            *string
	ChecksumSHA256          *string
	ChecksumSHA512          *string
	ChecksumXXHASH64        *string
	ChecksumXXHASH3         *string
	ChecksumXXHASH128       *string
}

// Options configures an S3BillingArchive. Prefix is a pointer so nil can mean
// the default "bursar" while an explicit empty string stores at the bucket root.
type Options struct {
	Bucket         string
	Region         string
	Credentials    *Credentials
	Endpoint       string
	ForcePathStyle bool
	Prefix         *string
	Client         Client
	ClientFactory  ClientFactory
	// OwnsClient defaults to false for Client and true for factory/default
	// clients. It matters only when the client also implements Close() error.
	OwnsClient *bool
	PutObject  PutObjectOptions
}

// S3BillingArchive archives billing webhook envelopes under deterministic,
// tenant-scoped keys using the official AWS SDK for Go v2.
type S3BillingArchive struct {
	bucket     string
	prefix     string
	factory    ClientFactory
	ownsClient bool
	putObject  PutObjectOptions
	mu         sync.Mutex
	client     Client
}

var (
	_ bursar.BillingPayloadArchive = (*S3BillingArchive)(nil)
	_ bursar.RuntimeComponent      = (*S3BillingArchive)(nil)
)

// NewBillingArchive validates configuration without contacting S3. The AWS
// region and credential provider chains are resolved lazily on first archive.
func NewBillingArchive(options Options) (*S3BillingArchive, error) {
	bucket, err := requireNonEmpty(options.Bucket, "S3 bucket")
	if err != nil {
		return nil, err
	}
	if options.Client != nil && options.ClientFactory != nil {
		return nil, errors.New("S3 client and client factory are mutually exclusive")
	}

	prefix := "bursar"
	if options.Prefix != nil {
		prefix = strings.Trim(strings.TrimSpace(*options.Prefix), "/")
	}
	putObject, err := normalizePutObjectOptions(options.PutObject)
	if err != nil {
		return nil, err
	}

	archive := &S3BillingArchive{
		bucket:    bucket,
		prefix:    prefix,
		client:    options.Client,
		putObject: putObject,
	}
	defaultOwnership := options.Client == nil
	archive.ownsClient = boolOption(options.OwnsClient, defaultOwnership)

	switch {
	case options.Client != nil:
		archive.factory = func(context.Context) (Client, error) { return options.Client, nil }
	case options.ClientFactory != nil:
		archive.factory = options.ClientFactory
	default:
		region, err := normalizeOptional(options.Region, "S3 region")
		if err != nil {
			return nil, err
		}
		endpoint, err := normalizeOptional(options.Endpoint, "S3 endpoint")
		if err != nil {
			return nil, err
		}
		staticCredentials, err := normalizeCredentials(options.Credentials)
		if err != nil {
			return nil, err
		}
		archive.factory = func(ctx context.Context) (Client, error) {
			loadOptions := make([]func(*awsconfig.LoadOptions) error, 0, 2)
			if region != "" {
				loadOptions = append(loadOptions, awsconfig.WithRegion(region))
			}
			if staticCredentials != nil {
				provider := credentials.NewStaticCredentialsProvider(
					staticCredentials.AccessKeyID,
					staticCredentials.SecretAccessKey,
					staticCredentials.SessionToken,
				)
				loadOptions = append(loadOptions, awsconfig.WithCredentialsProvider(provider))
			}
			configuration, err := awsconfig.LoadDefaultConfig(ctx, loadOptions...)
			if err != nil {
				return nil, fmt.Errorf("load AWS configuration for billing archive: %w", err)
			}
			return awss3.NewFromConfig(configuration, func(clientOptions *awss3.Options) {
				clientOptions.UsePathStyle = options.ForcePathStyle
				if endpoint != "" {
					clientOptions.BaseEndpoint = aws.String(endpoint)
				}
			}), nil
		}
	}
	return archive, nil
}

// Archive writes one canonical envelope and returns the durable object pointer.
func (a *S3BillingArchive) Archive(ctx context.Context, event bursar.BillingEventPayloadExport) (bursar.BillingPayloadArchiveResult, error) {
	if a == nil {
		return bursar.BillingPayloadArchiveResult{}, errors.New("S3 billing archive is not initialized")
	}
	if err := ctx.Err(); err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	if event.Envelope == nil {
		return bursar.BillingPayloadArchiveResult{}, fmt.Errorf("billing event %s has no PostgreSQL payload to archive", event.EventID)
	}
	tenantID, err := canonicalUUID(event.TenantID, "billing event tenant ID")
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	eventID, err := canonicalUUID(event.EventID, "billing event ID")
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	provider, err := requireNonEmpty(event.Provider, "billing event provider")
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	providerEnvironment, err := requireNonEmpty(event.ProviderEnvironment, "billing event provider environment")
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	providerEventID, err := requireNonEmpty(event.ProviderEventID, "billing provider event ID")
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	eventType, err := requireNonEmpty(event.EventType, "billing event type")
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	if event.ReceivedAt.IsZero() {
		return bursar.BillingPayloadArchiveResult{}, fmt.Errorf("billing event %s has an invalid received timestamp", eventID)
	}

	receivedAt := event.ReceivedAt.UTC()
	keyParts := []string{a.prefix, "tenants", tenantID, "billing-events", receivedAt.Format("2006/01/02"), eventID + ".json"}
	nonEmptyParts := keyParts[:0]
	for _, part := range keyParts {
		if part != "" {
			nonEmptyParts = append(nonEmptyParts, part)
		}
	}
	key := strings.Join(nonEmptyParts, "/")

	document := billingEnvelopeDocument{
		Schema:              "bursar.billing-event-envelope.v1",
		TenantID:            tenantID,
		EventID:             eventID,
		Provider:            provider,
		ProviderEnvironment: providerEnvironment,
		ProviderEventID:     providerEventID,
		EventType:           eventType,
		ReceivedAt:          receivedAt.Format(time.RFC3339Nano),
		Envelope:            event.Envelope,
	}
	if event.CompletedAt != nil {
		if event.CompletedAt.IsZero() {
			return bursar.BillingPayloadArchiveResult{}, fmt.Errorf("billing event %s has an invalid completed timestamp", eventID)
		}
		completedAt := event.CompletedAt.UTC().Format(time.RFC3339Nano)
		document.CompletedAt = &completedAt
	}
	body, err := json.Marshal(document)
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, fmt.Errorf("encode billing archive envelope: %w", err)
	}

	client, err := a.getClient(ctx)
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, err
	}
	input := a.putObjectInput(key, body, tenantID, eventID, provider, providerEnvironment)
	result, err := client.PutObject(ctx, input)
	if err != nil {
		return bursar.BillingPayloadArchiveResult{}, fmt.Errorf("archive billing event in S3: %w", err)
	}
	if result == nil {
		return bursar.BillingPayloadArchiveResult{}, errors.New("S3 PutObject returned no result")
	}
	var versionID *string
	if result.VersionId != nil {
		value := *result.VersionId
		versionID = &value
	}
	return bursar.BillingPayloadArchiveResult{Key: key, VersionID: versionID}, nil
}

// Start keeps client creation lazy; it exists so the archive can participate in
// BursarRuntime ownership when desired.
func (*S3BillingArchive) Start(ctx context.Context) error { return ctx.Err() }

// Flush is a no-op because PutObject completes before Archive returns.
func (*S3BillingArchive) Flush(ctx context.Context) error { return ctx.Err() }

// Close releases an owned custom client when it exposes Close() error. The AWS
// SDK's standard S3 client has no close requirement.
func (a *S3BillingArchive) Close(ctx context.Context) error {
	if a == nil {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	a.mu.Lock()
	client := a.client
	a.client = nil
	a.mu.Unlock()
	if !a.ownsClient || client == nil {
		return nil
	}
	if closer, ok := client.(interface{ Close() error }); ok {
		return closer.Close()
	}
	return nil
}

func (a *S3BillingArchive) getClient(ctx context.Context) (Client, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.client != nil {
		return a.client, nil
	}
	client, err := a.factory(ctx)
	if err != nil {
		return nil, fmt.Errorf("create S3 billing archive client: %w", err)
	}
	if client == nil {
		return nil, errors.New("S3 client factory returned nil")
	}
	a.client = client
	return client, nil
}

func (a *S3BillingArchive) putObjectInput(key string, body []byte, tenantID, eventID, provider, environment string) *awss3.PutObjectInput {
	options := a.putObject
	return &awss3.PutObjectInput{
		Bucket:                  aws.String(a.bucket),
		Key:                     aws.String(key),
		Body:                    bytes.NewReader(body),
		ContentType:             aws.String("application/json"),
		Metadata:                map[string]string{"bursar-tenant-id": tenantID, "bursar-event-id": eventID, "bursar-provider": provider, "bursar-environment": environment},
		ServerSideEncryption:    options.ServerSideEncryption,
		SSEKMSKeyId:             options.SSEKMSKeyID,
		SSEKMSEncryptionContext: options.SSEKMSEncryptionContext,
		BucketKeyEnabled:        options.BucketKeyEnabled,
		ChecksumAlgorithm:       options.ChecksumAlgorithm,
		ChecksumCRC32:           options.ChecksumCRC32,
		ChecksumCRC32C:          options.ChecksumCRC32C,
		ChecksumCRC64NVME:       options.ChecksumCRC64NVME,
		ChecksumMD5:             options.ChecksumMD5,
		ChecksumSHA1:            options.ChecksumSHA1,
		ChecksumSHA256:          options.ChecksumSHA256,
		ChecksumSHA512:          options.ChecksumSHA512,
		ChecksumXXHASH64:        options.ChecksumXXHASH64,
		ChecksumXXHASH3:         options.ChecksumXXHASH3,
		ChecksumXXHASH128:       options.ChecksumXXHASH128,
	}
}

type billingEnvelopeDocument struct {
	Schema              string         `json:"schema"`
	TenantID            string         `json:"tenantId"`
	EventID             string         `json:"eventId"`
	Provider            string         `json:"provider"`
	ProviderEnvironment string         `json:"providerEnvironment"`
	ProviderEventID     string         `json:"providerEventId"`
	EventType           string         `json:"eventType"`
	ReceivedAt          string         `json:"receivedAt"`
	CompletedAt         *string        `json:"completedAt"`
	Envelope            map[string]any `json:"envelope"`
}

func normalizePutObjectOptions(options PutObjectOptions) (PutObjectOptions, error) {
	var err error
	for name, destination := range map[string]**string{
		"SSE KMS key ID":             &options.SSEKMSKeyID,
		"SSE KMS encryption context": &options.SSEKMSEncryptionContext,
		"checksum CRC32":             &options.ChecksumCRC32,
		"checksum CRC32C":            &options.ChecksumCRC32C,
		"checksum CRC64NVME":         &options.ChecksumCRC64NVME,
		"checksum MD5":               &options.ChecksumMD5,
		"checksum SHA1":              &options.ChecksumSHA1,
		"checksum SHA256":            &options.ChecksumSHA256,
		"checksum SHA512":            &options.ChecksumSHA512,
		"checksum XXHASH64":          &options.ChecksumXXHASH64,
		"checksum XXHASH3":           &options.ChecksumXXHASH3,
		"checksum XXHASH128":         &options.ChecksumXXHASH128,
	} {
		*destination, err = normalizeOptionalPointer(*destination, name)
		if err != nil {
			return PutObjectOptions{}, err
		}
	}
	return options, nil
}

func normalizeCredentials(value *Credentials) (*Credentials, error) {
	if value == nil {
		return nil, nil
	}
	accessKeyID, err := requireNonEmpty(value.AccessKeyID, "S3 access key ID")
	if err != nil {
		return nil, err
	}
	secretAccessKey, err := requireNonEmpty(value.SecretAccessKey, "S3 secret access key")
	if err != nil {
		return nil, err
	}
	sessionToken := ""
	if value.SessionToken != "" {
		sessionToken, err = requireNonEmpty(value.SessionToken, "S3 session token")
		if err != nil {
			return nil, err
		}
	}
	return &Credentials{AccessKeyID: accessKeyID, SecretAccessKey: secretAccessKey, SessionToken: sessionToken}, nil
}

func normalizeOptional(value, name string) (string, error) {
	if value == "" {
		return "", nil
	}
	return requireNonEmpty(value, name)
}

func normalizeOptionalPointer(value *string, name string) (*string, error) {
	if value == nil {
		return nil, nil
	}
	normalized, err := requireNonEmpty(*value, name)
	if err != nil {
		return nil, err
	}
	return &normalized, nil
}

func requireNonEmpty(value, name string) (string, error) {
	normalized := strings.TrimSpace(value)
	if normalized == "" {
		return "", fmt.Errorf("%s must not be empty", name)
	}
	return normalized, nil
}

func canonicalUUID(value, name string) (string, error) {
	parsed, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil {
		return "", fmt.Errorf("%s must be a UUID: %w", name, err)
	}
	return parsed.String(), nil
}

func boolOption(value *bool, fallback bool) bool {
	if value == nil {
		return fallback
	}
	return *value
}
