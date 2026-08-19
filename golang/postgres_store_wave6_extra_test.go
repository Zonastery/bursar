package bursar

import (
	"context"
	"encoding/json"
	"errors"
	"math/big"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestPostgresStoreScalarAndProjectionHelpers(t *testing.T) {
	if row, err := rowRequired([]map[string]any{{"value": 1}}, "required"); err != nil || row["value"] != 1 {
		t.Fatalf("rowRequired() = %#v, %v", row, err)
	}
	for _, rows := range [][]map[string]any{nil, {nil}} {
		if _, err := rowRequired(rows, "required"); err == nil {
			t.Errorf("rowRequired(%#v) accepted", rows)
		}
	}
	for _, tc := range []struct {
		name string
		rows []map[string]any
		want bool
	}{
		{"empty", nil, false},
		{"all null", []map[string]any{{"value": nil}}, false},
		{"value", []map[string]any{{"value": 1, "other": nil}}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := rowOptional(tc.rows); (got != nil) != tc.want {
				t.Fatalf("rowOptional() = %#v, want present=%v", got, tc.want)
			}
		})
	}
	if value, err := firstScalar(map[string]any{"value": "ok"}, "scalar"); err != nil || value != "ok" {
		t.Fatalf("firstScalar() = %#v, %v", value, err)
	}
	for _, row := range []map[string]any{nil, map[string]any{}, map[string]any{"a": 1, "b": 2}} {
		if _, err := firstScalar(row, "scalar"); err == nil {
			t.Errorf("firstScalar(%#v) accepted", row)
		}
	}

	for _, tc := range []struct {
		name  string
		value any
		want  bool
	}{
		{"bool", true, true},
		{"string", "true", true},
		{"bytes", []byte("false"), false},
		{"invalid", "yes", false},
		{"nil", nil, false},
	} {
		t.Run("bool/"+tc.name, func(t *testing.T) {
			got, err := rowBool(map[string]any{"value": tc.value}, "value", "bool")
			if tc.name == "invalid" || tc.name == "nil" {
				if err == nil {
					t.Fatal("invalid bool accepted")
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("rowBool() = %v, %v", got, err)
			}
		})
	}
	for _, value := range []any{int(1), int8(2), int16(3), int32(4), int64(5), uint(6), uint32(7), uint64(8), "9", []byte("10")} {
		if got, err := rowInt(map[string]any{"value": value}, "value", "int"); err != nil || got < 1 {
			t.Errorf("rowInt(%T) = %d, %v", value, got, err)
		}
	}
	for _, value := range []any{float64(1), "bad", []byte("bad"), uint64(1) << 63} {
		if _, err := rowInt(map[string]any{"value": value}, "value", "int"); err == nil {
			t.Errorf("rowInt(%v) accepted", value)
		}
	}

	when := time.Date(2026, 8, 19, 12, 13, 14, 0, time.FixedZone("IST", 19800))
	for _, value := range []any{when, when.Format(time.RFC3339Nano), []byte(when.Format(time.RFC3339Nano))} {
		got, err := rowTime(map[string]any{"at": value}, "at", "time")
		if err != nil || !got.Equal(when.UTC()) {
			t.Errorf("rowTime(%T) = %v, %v", value, got, err)
		}
	}
	if _, err := rowTime(map[string]any{"at": "bad"}, "at", "time"); err == nil {
		t.Fatal("invalid timestamp accepted")
	}
}

func TestPostgresStoreAmountAndJSONBoundaries(t *testing.T) {
	jsonNumber := json.Number("1.230000")
	for _, tc := range []struct {
		name  string
		value any
		want  string
	}{
		{"string", "1.000001", "1.000001"},
		{"json number", jsonNumber, "1.230000"},
		{"float", float64(2.5), "2.500000"},
		{"bytes", []byte("3.25"), "3.250000"},
		{"int", int64(4), "4.000000"},
		{"numeric", pgtype.Numeric{Int: big.NewInt(1234), Exp: -3, Valid: true, InfinityModifier: pgtype.Finite}, "1.234000"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseAmount(tc.value, "amount")
			if err != nil || got.StringFixed(MoneyDecimalPlaces) != tc.want {
				t.Fatalf("parseAmount() = %s, %v; want %s", got, err, tc.want)
			}
		})
	}
	for _, value := range []any{nil, "not-money", pgtype.Numeric{Valid: false}, pgtype.Numeric{Int: big.NewInt(1), Valid: true, NaN: true}, pgtype.Numeric{Int: big.NewInt(1), Valid: true, InfinityModifier: pgtype.Infinity}} {
		if _, err := parseAmount(value, "amount"); err == nil {
			t.Errorf("parseAmount(%#v) accepted", value)
		}
	}
	for _, tc := range []struct {
		integer  string
		exponent int32
		want     string
	}{
		{"123", 2, "12300"},
		{"123", -2, "1.23"},
		{"123", -5, "0.00123"},
		{"-123", -2, "-1.23"},
	} {
		if got := numericText(new(big.Int).SetInt64(0), 0); got != "0" {
			t.Fatalf("numericText zero = %q", got)
		}
		integer, _ := new(big.Int).SetString(tc.integer, 10)
		if got := numericText(integer, tc.exponent); got != tc.want {
			t.Errorf("numericText(%s, %d) = %q, want %q", tc.integer, tc.exponent, got, tc.want)
		}
	}

	original := map[string]any{"number": json.Number("1.25"), "nested": map[string]any{"ok": true}}
	mapped, err := jsonMap(original, "document")
	if err != nil || mapped == nil {
		t.Fatalf("jsonMap(map) = %#v, %v", mapped, err)
	}
	mapped["new"] = true
	if _, exists := original["new"]; exists {
		t.Fatal("jsonMap returned an aliased map")
	}
	for _, value := range []any{`{"amount":1.25}`, []byte(`{"amount":1.25}`)} {
		mapped, err := jsonMap(value, "document")
		if err != nil || mapped["amount"] != json.Number("1.25") {
			t.Fatalf("jsonMap(%T) = %#v, %v", value, mapped, err)
		}
	}
	for _, value := range []any{"[]", 42, "{"} {
		if _, err := jsonMap(value, "document"); err == nil {
			t.Errorf("jsonMap(%#v) accepted", value)
		}
	}

	if optional, err := optionalRowAmount(map[string]any{}, "amount", "row"); err != nil || optional != nil {
		t.Fatalf("optionalRowAmount(missing) = %#v, %v", optional, err)
	}
	if optional, err := optionalRowAmount(map[string]any{"amount": "1.25"}, "amount", "row"); err != nil || optional == nil {
		t.Fatalf("optionalRowAmount(value) = %#v, %v", optional, err)
	}
	if optional, err := optionalRowTime(map[string]any{}, "at", "row"); err != nil || optional != nil {
		t.Fatalf("optionalRowTime(missing) = %#v, %v", optional, err)
	}
	if optional, err := optionalRowInt(map[string]any{}, "count", "row"); err != nil || optional != nil {
		t.Fatalf("optionalRowInt(missing) = %#v, %v", optional, err)
	}
}

func TestPostgresStoreListAndPayloadHelpers(t *testing.T) {
	for _, tc := range []struct {
		name  string
		value any
		want  []string
	}{
		{"strings", []string{"a", "b"}, []string{"a", "b"}},
		{"any", []any{"a", 2}, []string{"a", "2"}},
		{"json", `["a","b"]`, []string{"a", "b"}},
		{"bytes", []byte(`["a"]`), []string{"a"}},
	} {
		t.Run("strings/"+tc.name, func(t *testing.T) {
			got, err := stringSlice(tc.value, "items")
			if err != nil || len(got) != len(tc.want) {
				t.Fatalf("stringSlice() = %#v, %v", got, err)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("stringSlice() = %#v, want %#v", got, tc.want)
				}
			}
		})
	}
	for _, value := range []any{[]any{nil}, `not-json`, 42} {
		if _, err := stringSlice(value, "items"); err == nil {
			t.Errorf("stringSlice(%#v) accepted", value)
		}
	}
	for _, value := range []any{[]float64{1, 2}, []any{float64(1), float32(2), int(3), int64(4), "5", []byte("6")}} {
		got, err := floatSlice(value, "values")
		if err != nil || len(got) != 2 && len(got) != 6 {
			t.Errorf("floatSlice(%#v) = %#v, %v", value, got, err)
		}
	}
	for _, value := range []any{[]any{"bad"}, "bad", 42} {
		if _, err := floatSlice(value, "values"); err == nil {
			t.Errorf("floatSlice(%#v) accepted", value)
		}
	}

	metadata, err := marshalMetadata(CreditMetadata{"source": "test"})
	if err != nil || string(metadata) != `{"source":"test"}` {
		t.Fatalf("marshalMetadata() = %s, %v", metadata, err)
	}
	measureJSON, measureErr := marshalMeasures(nil)
	dimensionJSON, dimensionErr := marshalDimensions(nil)
	for _, tc := range []struct {
		name string
		got  json.RawMessage
		want string
	}{
		{"nil measures", measureJSON, `{}`},
		{"nil dimensions", dimensionJSON, `{}`},
	} {
		if (tc.name == "nil measures" && measureErr != nil) || (tc.name == "nil dimensions" && dimensionErr != nil) {
			t.Fatalf("marshal helper error: %v/%v", measureErr, dimensionErr)
		}
		if string(tc.got) != tc.want {
			t.Errorf("%s = %s, want %s", tc.name, tc.got, tc.want)
		}
	}
	if amountArgument(MustAmount("1.2")) != "1.200000" {
		t.Fatal("amountArgument did not quantize")
	}
	if nullableText(" ") != nil || nullableText("x") != "x" {
		t.Fatal("nullableText mismatch")
	}
	when := time.Now().Add(time.Hour)
	if nullableTime(nil) != nil || !nullableTime(&when).(time.Time).Equal(when.UTC()) {
		t.Fatal("nullableTime mismatch")
	}
	metadataJSON, measuresJSON, dimensionsJSON, err := operationPayload(OperationUsageOptions{Model: "model", Region: "region", Measures: map[string]Amount{"tokens": MustAmount("1")}}, CreditMetadata{})
	if err != nil || len(metadataJSON) == 0 || len(measuresJSON) == 0 || len(dimensionsJSON) == 0 {
		t.Fatalf("operationPayload() = %s/%s/%s, %v", metadataJSON, measuresJSON, dimensionsJSON, err)
	}
}

func TestPostgresStoreProjectionAndPaginationBoundaries(t *testing.T) {
	if empty, err := balanceFromState("user", nil, "balance"); err != nil || !empty.Balance.Equal(DecimalZero) {
		t.Fatalf("empty balance = %#v, %v", empty, err)
	}
	state, err := balanceFromState("user", map[string]any{"balance": "4.25", "lifetime_purchased": "9.5"}, "balance")
	if err != nil || !state.Balance.Equal(MustAmount("4.25")) || !state.LifetimePurchased.Equal(MustAmount("9.5")) {
		t.Fatalf("balance state = %#v, %v", state, err)
	}

	revision, err := catalogRevisionFromRow(map[string]any{"id": storageTestBilling, "revision_no": 3, "source_document": `{"plans":{}}`}, "catalog")
	if err != nil || revision.ID != storageTestBilling || revision.Version != 3 || revision.Config["plans"] == nil {
		t.Fatalf("catalog revision = %#v, %v", revision, err)
	}
	if _, err := catalogRevisionFromRow(map[string]any{"id": storageTestBilling, "revision_no": 3, "source_document": nil}, "catalog"); err == nil {
		t.Fatal("empty catalog document accepted")
	}
	rollout := defaultCatalogRollout(CatalogRollout{Plans: map[string]PlanRollout{"pro": {Effective: "immediate", IncludePinned: true}}})
	plans, ok := rollout["plans"].(map[string]any)
	if !ok || plans["pro"] == nil {
		t.Fatalf("default rollout = %#v", rollout)
	}

	for _, tc := range []struct {
		name  string
		value any
		want  string
	}{
		{"timestamp", time.Date(2026, 8, 19, 12, 0, 0, 0, time.FixedZone("IST", 19800)), "2026-08-19"},
		{"text", "2026-08-20", "2026-08-20"},
	} {
		t.Run("calendar/"+tc.name, func(t *testing.T) {
			got, err := rowCalendarDate(map[string]any{"day": tc.value}, "day", "calendar")
			if err != nil || got.Format("2006-01-02") != tc.want {
				t.Fatalf("rowCalendarDate() = %v, %v", got, err)
			}
		})
	}
	if _, err := rowCalendarDate(map[string]any{"day": "bad"}, "day", "calendar"); err == nil {
		t.Fatal("invalid calendar date accepted")
	}
	if rowTextFallback(map[string]any{"subject_id": "subject"}, "subject_id", "user_id") != "subject" || rowTextFallback(map[string]any{"user_id": "user"}, "subject_id", "user_id") != "user" {
		t.Fatal("rowTextFallback did not select the available identity")
	}

	now := time.Date(2026, 8, 19, 0, 0, 0, 0, time.UTC)
	if err := requireTimeRange(now, now.Add(time.Hour), "range"); err != nil {
		t.Fatal(err)
	}
	for _, tc := range [][2]time.Time{{time.Time{}, now}, {now.Add(time.Hour), now}} {
		if err := requireTimeRange(tc[0], tc[1], "range"); err == nil {
			t.Errorf("invalid range %#v accepted", tc)
		}
	}
	if _, err := validateLedgerOptions(ListLedgerEntriesOptions{Limit: 1, From: ptrTime(now.Add(time.Hour)), To: ptrTime(now)}); err == nil {
		t.Fatal("backward ledger range accepted")
	}
	if limit, err := validateLedgerOptions(ListLedgerEntriesOptions{}); err != nil || limit != defaultPageSize {
		t.Fatalf("default ledger limit = %d, %v", limit, err)
	}
}

func ptrTime(value time.Time) *time.Time { return &value }

func TestPostgresStoreOptionAndLifecycleSafety(t *testing.T) {
	if _, _, _, err := postgresStoreClientOptions(PostgresStoreOptions{TenantID: "bad", ProviderEnvironment: ProviderEnvironmentTest}); err == nil {
		t.Fatal("invalid tenant accepted")
	}
	if _, _, _, err := postgresStoreClientOptions(PostgresStoreOptions{TenantID: storageTestTenant}); err == nil {
		t.Fatal("missing provider environment accepted")
	}
	if _, err := NewPostgresStoreFromPool(nil, PostgresStoreOptions{}); err == nil {
		t.Fatal("nil pool accepted")
	}
	if _, err := NewPostgresStore(context.Background(), "", PostgresStoreOptions{TenantID: storageTestTenant, ProviderEnvironment: ProviderEnvironmentTest}); err == nil {
		t.Fatal("missing database URL accepted")
	}
	if _, err := NewPostgresStore(context.Background(), "not a postgres URL", PostgresStoreOptions{TenantID: storageTestTenant, ProviderEnvironment: ProviderEnvironmentTest}); err == nil {
		t.Fatal("invalid database URL accepted")
	}
	if (*PostgresStore)(nil).TenantID() != "" || (*PostgresStore)(nil).ProviderEnvironment() != "" || (*PostgresStore)(nil).Close() != nil {
		t.Fatal("nil store lifecycle is not safe")
	}
	if _, err := (*PostgresStore)(nil).DatabaseURL(); err == nil {
		t.Fatal("nil store DatabaseURL succeeded")
	}
	if err := (*PostgresStore)(nil).withTx(context.Background(), nil); err == nil {
		t.Fatal("nil store transaction succeeded")
	}

	store, err := NewPostgresStoreFromPool(&pgxpool.Pool{}, PostgresStoreOptions{TenantID: storageTestTenant, ProviderEnvironment: ProviderEnvironmentTest})
	if err != nil {
		t.Fatal(err)
	}
	if store.TenantID() != storageTestTenant || store.ProviderEnvironment() != ProviderEnvironmentTest {
		t.Fatalf("store identity = %q/%q", store.TenantID(), store.ProviderEnvironment())
	}
	if _, err := store.DatabaseURL(); err == nil {
		t.Fatal("borrowed store exposed database URL")
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestPostgresClientPureLifecycleAndErrorBranches(t *testing.T) {
	if _, err := NewPostgresClient(context.Background(), "", PostgresClientOptions{}); err == nil {
		t.Fatal("empty database URL accepted")
	}
	if _, err := NewPostgresClientFromPool(nil, PostgresClientOptions{}); err == nil {
		t.Fatal("nil pool accepted")
	}
	client, err := NewPostgresClientFromPool(&pgxpool.Pool{}, PostgresClientOptions{TenantID: storageTestTenant})
	if err != nil {
		t.Fatal(err)
	}
	if client.TenantID() != storageTestTenant || client.OwnsPool() {
		t.Fatalf("client identity = %q, owns=%v", client.TenantID(), client.OwnsPool())
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	if err := client.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Call(context.Background(), "get_credit_state"); err == nil {
		t.Fatal("closed client Call succeeded")
	}
	if _, err := client.Query(context.Background(), "SELECT 1"); err == nil {
		t.Fatal("closed client Query succeeded")
	}
	if _, err := (*PostgresClient)(nil).Call(context.Background(), "get_credit_state"); err == nil {
		t.Fatal("nil client Call succeeded")
	}
	if _, err := (*PostgresClient)(nil).Query(context.Background(), "SELECT 1"); err == nil {
		t.Fatal("nil client Query succeeded")
	}
	for _, tx := range []*PostgresTransaction{nil, &PostgresTransaction{}, &PostgresTransaction{tx: nil}} {
		if _, err := tx.Call(context.Background(), "get_credit_state"); err == nil {
			t.Error("inactive transaction Call succeeded")
		}
		if _, err := tx.Query(context.Background(), "SELECT 1"); err == nil {
			t.Error("inactive transaction Query succeeded")
		}
	}
	if _, err := (&PostgresTransaction{tx: nil}).call(context.Background(), "NotValid"); err == nil {
		t.Fatal("invalid RPC accepted by inactive transaction")
	}
	for _, pgError := range []*pgconn.PgError{{Code: "57014"}, {Code: "08006"}, {Code: "23505"}} {
		if normalizePostgresError(pgError, "query", true) == nil {
			t.Fatalf("PostgreSQL error %s was not classified", pgError.Code)
		}
	}
	if got := normalizePostgresError(errors.New("store failed"), "query", true); got == nil {
		t.Fatal("generic error was not classified")
	}
}
