// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type stubChecker struct{ err error }

func (s stubChecker) ReadinessCheck(context.Context) error { return s.err }

func newTestServer(t *testing.T, checker ReadinessChecker) *httptest.Server {
	t.Helper()
	h := NewHandlers(checker, VersionInfo{Version: "vTest", Commit: "abc1234", Date: "2026-08-21"},
		slog.New(slog.NewTextHandler(io.Discard, nil)))
	srv := httptest.NewServer(Routes(h, time.Second))
	t.Cleanup(srv.Close)
	return srv
}

func getBody(t *testing.T, url string) (int, http.Header, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return resp.StatusCode, resp.Header, string(body)
}

func assertJSONBody(t *testing.T, body, want string) {
	t.Helper()
	if got := strings.TrimSpace(body); got != want {
		t.Errorf("body = %q, want %q", got, want)
	}
}

func TestLiveness(t *testing.T) {
	srv := newTestServer(t, nil)
	status, header, body := getBody(t, srv.URL+"/healthz")
	if status != http.StatusOK {
		t.Errorf("status = %d, want %d", status, http.StatusOK)
	}
	if got := header.Get("Content-Type"); got != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", got)
	}
	assertJSONBody(t, body, `{"status":"ok"}`)
}

func TestReadiness(t *testing.T) {
	t.Run("ready", func(t *testing.T) {
		srv := newTestServer(t, stubChecker{})
		status, _, body := getBody(t, srv.URL+"/readyz")
		if status != http.StatusOK {
			t.Errorf("status = %d, want %d", status, http.StatusOK)
		}
		assertJSONBody(t, body, `{"status":"ok"}`)
	})
	t.Run("unavailable", func(t *testing.T) {
		srv := newTestServer(t, stubChecker{err: errors.New("db down")})
		status, _, body := getBody(t, srv.URL+"/readyz")
		if status != http.StatusServiceUnavailable {
			t.Errorf("status = %d, want %d", status, http.StatusServiceUnavailable)
		}
		assertJSONBody(t, body, `{"status":"unavailable"}`)
		if strings.Contains(body, "db down") {
			t.Error("response leaked checker error details")
		}
	})
}

func TestVersion(t *testing.T) {
	srv := newTestServer(t, nil)
	status, _, body := getBody(t, srv.URL+"/version")
	if status != http.StatusOK {
		t.Errorf("status = %d, want %d", status, http.StatusOK)
	}
	var got VersionInfo
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("decode version body %q: %v", body, err)
	}
	want := VersionInfo{Version: "vTest", Commit: "abc1234", Date: "2026-08-21"}
	if got != want {
		t.Errorf("version = %+v, want %+v", got, want)
	}
}

func TestNotFound(t *testing.T) {
	srv := newTestServer(t, nil)
	status, header, body := getBody(t, srv.URL+"/nope")
	if status != http.StatusNotFound {
		t.Errorf("status = %d, want %d", status, http.StatusNotFound)
	}
	if got := header.Get("Content-Type"); got != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", got)
	}
	assertJSONBody(t, body, `{"status":"not_found"}`)
}

func TestMethodNotAllowed(t *testing.T) {
	srv := newTestServer(t, nil)
	resp, err := http.Post(srv.URL+"/healthz", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatalf("POST /healthz: %v", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("status = %d, want %d", resp.StatusCode, http.StatusMethodNotAllowed)
	}
	if got := resp.Header.Get("Content-Type"); got != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", got)
	}
	assertJSONBody(t, string(body), `{"status":"method_not_allowed"}`)
}

func TestRequestIDHeader(t *testing.T) {
	srv := newTestServer(t, nil)
	status, header, _ := getBody(t, srv.URL+"/healthz")
	if status != http.StatusOK {
		t.Fatalf("status = %d, want %d", status, http.StatusOK)
	}
	if id := header.Get("X-Request-Id"); id == "" {
		t.Error("X-Request-Id header missing or empty")
	}
}

func TestNewHandlersNilLoggerFallsBackToDefault(t *testing.T) {
	h := NewHandlers(nil, VersionInfo{}, nil)
	if h == nil {
		t.Fatal("NewHandlers returned nil")
	}
	if h.logger == nil {
		t.Error("NewHandlers left logger nil, want slog.Default fallback")
	}
}
