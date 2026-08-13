// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"

	"github.com/jackc/pgx/v5/pgtype"
)

func TestPostgresUUIDArgumentsPreserveUUIDTypeAndNullability(t *testing.T) {
	t.Parallel()

	const value = "4c9dc1cd-cc2f-43f5-8e10-607de8e741c8"
	required, err := postgresUUID(value, "test ID")
	if err != nil {
		t.Fatalf("postgresUUID() error = %v", err)
	}
	if !required.Valid || required.String() != value {
		t.Fatalf("postgresUUID() = %#v, want valid %s", required, value)
	}

	nullable, err := nullablePostgresUUID("", "test ID")
	if err != nil {
		t.Fatalf("nullablePostgresUUID(empty) error = %v", err)
	}
	if nullable.Valid {
		t.Fatalf("nullablePostgresUUID(empty) = %#v, want typed SQL NULL", nullable)
	}
	if _, ok := any(nullable).(pgtype.UUID); !ok {
		t.Fatalf("nullablePostgresUUID(empty) has type %T, want pgtype.UUID", nullable)
	}

	nullable, err = nullablePostgresUUID(value, "test ID")
	if err != nil {
		t.Fatalf("nullablePostgresUUID(value) error = %v", err)
	}
	if !nullable.Valid || nullable.String() != value {
		t.Fatalf("nullablePostgresUUID(value) = %#v, want valid %s", nullable, value)
	}
}

func TestPostgresUUIDArgumentsRejectTextThatIsNotUUID(t *testing.T) {
	t.Parallel()

	for _, call := range []struct {
		name string
		fn   func() (pgtype.UUID, error)
	}{
		{name: "required", fn: func() (pgtype.UUID, error) { return postgresUUID("not-a-uuid", "test ID") }},
		{name: "nullable non-empty", fn: func() (pgtype.UUID, error) { return nullablePostgresUUID("not-a-uuid", "test ID") }},
	} {
		call := call
		t.Run(call.name, func(t *testing.T) {
			_, err := call.fn()
			if err == nil {
				t.Fatal("invalid UUID text was accepted")
			}
			classified, ok := AsBursarError(err)
			if !ok || classified.Code != ErrorCodeConfig || classified.Category != ErrorCategoryInvalidRequest {
				t.Fatalf("error = %#v, want CONFIG_ERROR/invalid_request", err)
			}
		})
	}
}
