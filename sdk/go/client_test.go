package experimentation_test

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	exp "github.com/controlvar/controlvar/sdk/go"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func newTestUser(id string) *exp.User {
	return &exp.User{ID: id}
}

func newTestUserWithAttrs(id string, attrs map[string]interface{}) *exp.User {
	return &exp.User{ID: id, Attributes: attrs}
}

// recordedRequest is one request captured by the fake backend.
type recordedRequest struct {
	Method      string
	Path        string // decoded path
	EscapedPath string // as sent on the wire
	Query       map[string]string
	Header      http.Header
	Body        map[string]interface{}
	RawBody     []byte
}

// fakeBackend is an httptest.Server that records every request and answers
// from a route table keyed by "METHOD /path".
type fakeBackend struct {
	*httptest.Server
	mu       sync.Mutex
	requests []recordedRequest
	routes   map[string]http.HandlerFunc
}

func newFakeBackend(t *testing.T) *fakeBackend {
	t.Helper()
	fb := &fakeBackend{routes: map[string]http.HandlerFunc{}}
	fb.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := readAll(r)
		rec := recordedRequest{
			Method:      r.Method,
			Path:        r.URL.Path,
			EscapedPath: r.URL.EscapedPath(),
			Query:       map[string]string{},
			Header:      r.Header.Clone(),
			RawBody:     raw,
		}
		for k, v := range r.URL.Query() {
			rec.Query[k] = v[0]
		}
		if len(raw) > 0 {
			_ = json.Unmarshal(raw, &rec.Body)
		}
		fb.mu.Lock()
		fb.requests = append(fb.requests, rec)
		handler := fb.routes[r.Method+" "+r.URL.Path]
		if handler == nil {
			handler = fb.routes[r.Method+" *"]
		}
		fb.mu.Unlock()

		if handler == nil {
			http.Error(w, `{"detail":"no route"}`, http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		handler(w, r)
	}))
	t.Cleanup(fb.Close)
	return fb
}

func readAll(r *http.Request) ([]byte, error) {
	if r.Body == nil {
		return nil, nil
	}
	defer r.Body.Close()
	buf := make([]byte, 0, 512)
	tmp := make([]byte, 512)
	for {
		n, err := r.Body.Read(tmp)
		buf = append(buf, tmp[:n]...)
		if err != nil {
			break
		}
	}
	return buf, nil
}

func (fb *fakeBackend) handle(method, path string, h http.HandlerFunc) {
	fb.mu.Lock()
	defer fb.mu.Unlock()
	fb.routes[method+" "+path] = h
}

func (fb *fakeBackend) respondJSON(method, path string, status int, body interface{}) {
	fb.handle(method, path, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	})
}

func (fb *fakeBackend) Requests() []recordedRequest {
	fb.mu.Lock()
	defer fb.mu.Unlock()
	out := make([]recordedRequest, len(fb.requests))
	copy(out, fb.requests)
	return out
}

func (fb *fakeBackend) Count() int {
	fb.mu.Lock()
	defer fb.mu.Unlock()
	return len(fb.requests)
}

// Standard fixtures ---------------------------------------------------------

var assignResponse = map[string]interface{}{
	"experiment_key": "checkout_flow",
	"user_id":        "user-1",
	"variant_id":     "8b6f1c2e-0000-4000-8000-000000000001",
	"variant_name":   "treatment",
	"is_control":     false,
	"configuration":  map[string]interface{}{"headline": "Buy now", "discount": 10},
}

func (fb *fakeBackend) serveAssign() {
	fb.respondJSON(http.MethodPost, "/api/v1/tracking/assign", http.StatusOK, assignResponse)
}

func (fb *fakeBackend) serveFlag(key string, enabled bool, config interface{}) {
	fb.respondJSON(http.MethodGet, "/api/v1/feature-flags/evaluate/"+key, http.StatusOK,
		map[string]interface{}{"key": key, "enabled": enabled, "config": config})
}

func (fb *fakeBackend) serveTrack() {
	fb.respondJSON(http.MethodPost, "/api/v1/tracking/track", http.StatusOK, map[string]interface{}{"id": "evt-1"})
	fb.respondJSON(http.MethodPost, "/api/v1/tracking/batch", http.StatusOK,
		map[string]interface{}{"success_count": 1, "failure_count": 0, "errors": nil})
}

// mustNewClient creates a client pointed at a test server; fails the test on error.
func mustNewClient(t *testing.T, serverURL string, opts ...exp.Option) exp.Client {
	t.Helper()
	base := []exp.Option{
		exp.WithBaseURL(serverURL),
		exp.WithAPIKey("test-key"),
		exp.WithCacheSize(100),
		exp.WithCacheTTL(5 * time.Minute),
	}
	c, err := exp.New(append(base, opts...)...)
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c
}

func intToStr(n int) string {
	if n == 0 {
		return "0"
	}
	digits := []byte{}
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	return string(digits)
}

// ---------------------------------------------------------------------------
// ConsistentHash: golden vectors shared by every SDK
// (tests/sdk-contract/golden-vectors.json)
// ---------------------------------------------------------------------------

var goldenVectors = []struct {
	userID   string
	flagKey  string
	expected float64
}{
	{"user-123", "my-flag", 0.6927449859213084},
	{"alice", "dark-mode", 0.0353864398784935},
	{"bob", "new-checkout", 0.1463384565431625},
	{"user-789", "beta-feature", 0.3134219283238053},
	{"test-user", "flag-key", 0.9923393740318716},
	{"", "empty-user", 0.3582690393086523},
	{"a", "b", 0.6056532170623541},
	{"user-001", "exp-abc", 0.7764157797209918},
	{"user-002", "exp-abc", 0.1422550100833178},
}

func TestConsistentHash_GoldenVectors(t *testing.T) {
	for _, v := range goldenVectors {
		got := exp.ConsistentHash(v.userID, v.flagKey)
		if math.Abs(got-v.expected) > 1e-12 {
			t.Errorf("ConsistentHash(%q, %q) = %.16f; want %.16f", v.userID, v.flagKey, got, v.expected)
		}
	}
}

// TestConsistentHash_GoldenVectorFile replays tests/sdk-contract/golden-vectors.json
// when the checkout contains it (skipped when the SDK is vendored elsewhere).
func TestConsistentHash_GoldenVectorFile(t *testing.T) {
	path := filepath.Join("..", "..", "tests", "sdk-contract", "golden-vectors.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("golden vector file not available: %v", err)
	}
	var doc struct {
		HashVectors []struct {
			UserID       string  `json:"user_id"`
			FlagKey      string  `json:"flag_key"`
			ExpectedHash float64 `json:"expected_hash"`
		} `json:"hash_vectors"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("decode golden vectors: %v", err)
	}
	if len(doc.HashVectors) == 0 {
		t.Fatal("golden vector file has no hash_vectors")
	}
	for _, v := range doc.HashVectors {
		got := exp.ConsistentHash(v.UserID, v.FlagKey)
		if math.Abs(got-v.ExpectedHash) > 1e-12 {
			t.Errorf("ConsistentHash(%q, %q) = %.16f; want %.16f", v.UserID, v.FlagKey, got, v.ExpectedHash)
		}
	}
}

func TestConsistentHash_Deterministic(t *testing.T) {
	first := exp.ConsistentHash("user-repeat", "consistency-test")
	for i := 0; i < 100; i++ {
		if got := exp.ConsistentHash("user-repeat", "consistency-test"); got != first {
			t.Fatalf("call %d: got %v; want %v", i, got, first)
		}
	}
}

func TestConsistentHash_RangeAndDistribution(t *testing.T) {
	below := 0
	const total = 10000
	for i := 0; i < total; i++ {
		h := exp.ConsistentHash("dist-user-"+intToStr(i), "dist-flag")
		if h < 0 || h >= 1 {
			t.Fatalf("hash out of range: %v", h)
		}
		if h < 0.5 {
			below++
		}
	}
	fraction := float64(below) / float64(total)
	if fraction < 0.45 || fraction > 0.55 {
		t.Errorf("distribution out of range: %.2f%% below 0.5, want 45%%–55%%", fraction*100)
	}
}

// ---------------------------------------------------------------------------
// Cache tests
// ---------------------------------------------------------------------------

func TestCache_GetSet(t *testing.T) {
	c := exp.NewCache(10, time.Minute)

	c.Set("key1", "value1")
	val, ok := c.Get("key1")
	if !ok {
		t.Fatal("Get: expected ok=true")
	}
	if val != "value1" {
		t.Errorf("Get: got %v; want %q", val, "value1")
	}
}

func TestCache_MissReturnsNotFound(t *testing.T) {
	c := exp.NewCache(10, time.Minute)
	if _, ok := c.Get("nonexistent"); ok {
		t.Error("Get: expected ok=false for missing key")
	}
}

func TestCache_TTLExpiry(t *testing.T) {
	c := exp.NewCache(10, 50*time.Millisecond)
	c.Set("expiring", "soon")

	if _, ok := c.Get("expiring"); !ok {
		t.Fatal("expected key to exist before TTL")
	}

	time.Sleep(100 * time.Millisecond)

	if _, ok := c.Get("expiring"); ok {
		t.Error("expected key to be expired after TTL")
	}
}

func TestCache_LRUEviction(t *testing.T) {
	c := exp.NewCache(3, time.Minute)
	c.Set("a", 1)
	c.Set("b", 2)
	c.Set("c", 3)

	c.Get("a") // make "a" recently used

	c.Set("d", 4) // evicts "b"

	if _, ok := c.Get("b"); ok {
		t.Error("expected 'b' to be evicted (LRU)")
	}
	for _, k := range []string{"a", "c", "d"} {
		if _, ok := c.Get(k); !ok {
			t.Errorf("expected %q to still exist", k)
		}
	}
}

func TestCache_UpdateExistingKey(t *testing.T) {
	c := exp.NewCache(10, time.Minute)
	c.Set("key", "old")
	c.Set("key", "new")

	val, ok := c.Get("key")
	if !ok || val != "new" {
		t.Errorf("got %v, %v; want 'new', true", val, ok)
	}
	if c.Len() != 1 {
		t.Errorf("Len = %d; want 1", c.Len())
	}
}

func TestCache_Delete(t *testing.T) {
	c := exp.NewCache(10, time.Minute)
	c.Set("key", "value")
	c.Delete("key")
	c.Delete("nonexistent") // must not panic

	if _, ok := c.Get("key"); ok {
		t.Error("expected key to be deleted")
	}
}

func TestCache_Clear(t *testing.T) {
	c := exp.NewCache(10, time.Minute)
	c.Set("a", 1)
	c.Set("b", 2)
	c.Clear()

	if c.Len() != 0 {
		t.Errorf("Len = %d after Clear; want 0", c.Len())
	}
}

func TestCache_Range(t *testing.T) {
	c := exp.NewCache(10, 50*time.Millisecond)
	c.Set("a", 1)
	c.Set("b", 2)
	time.Sleep(100 * time.Millisecond)
	c.Set("c", 3)

	seen := map[string]interface{}{}
	c.Range(func(key string, value interface{}) {
		seen[key] = value
		c.Get(key) // re-entrancy must not deadlock
	})

	if len(seen) != 1 || seen["c"] != 3 {
		t.Errorf("Range saw %v; want only c=3 (expired entries dropped)", seen)
	}
	if c.Len() != 1 {
		t.Errorf("Len = %d after Range; want 1 (expired entries removed)", c.Len())
	}
}

func TestCache_Concurrent(t *testing.T) {
	c := exp.NewCache(100, time.Minute)
	var wg sync.WaitGroup
	const goroutines = 50
	const ops = 100

	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for j := 0; j < ops; j++ {
				key := "key-" + intToStr(id*ops+j)
				c.Set(key, j)
				c.Get(key)
				if j%10 == 0 {
					c.Delete(key)
				}
				if j%25 == 0 {
					c.Range(func(string, interface{}) {})
				}
			}
		}(i)
	}
	wg.Wait()
}

func TestCache_NoTTL(t *testing.T) {
	c := exp.NewCache(10, 0) // 0 TTL = no expiry
	c.Set("key", "value")
	time.Sleep(10 * time.Millisecond)

	if val, ok := c.Get("key"); !ok || val != "value" {
		t.Errorf("got %v, %v; want 'value', true", val, ok)
	}
}

// ---------------------------------------------------------------------------
// Config / functional options tests
// ---------------------------------------------------------------------------

func TestNew_Defaults(t *testing.T) {
	c, err := exp.New()
	if err != nil {
		t.Fatalf("New() with default config should not error; got: %v", err)
	}
	_ = c.Close()
}

func TestNew_EmptyBaseURL(t *testing.T) {
	if _, err := exp.New(exp.WithBaseURL("")); err == nil {
		t.Error("expected error for empty BaseURL")
	}
}

func TestNew_TrimsTrailingSlash(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("f", true, nil)

	c := mustNewClient(t, fb.URL+"/")
	if _, err := c.EvaluateFlag(context.Background(), "f", newTestUser("u1")); err != nil {
		t.Fatalf("EvaluateFlag error: %v", err)
	}
	if got := fb.Requests()[0].Path; got != "/api/v1/feature-flags/evaluate/f" {
		t.Errorf("path = %q; want no double slash", got)
	}
}

func TestNew_AllOptions(t *testing.T) {
	c, err := exp.New(
		exp.WithBaseURL("http://localhost:8000"),
		exp.WithAPIKey("key-123"),
		exp.WithTimeout(5*time.Second),
		exp.WithCacheSize(200),
		exp.WithCacheTTL(2*time.Minute),
		exp.WithLocalEval(true), // deprecated no-op, must still compile and be accepted
	)
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	defer c.Close()
}

func TestWithTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
		_, _ = w.Write([]byte(`{"key":"f","enabled":true}`))
	}))
	defer srv.Close()

	c := mustNewClient(t, srv.URL, exp.WithTimeout(50*time.Millisecond))
	result, err := c.EvaluateFlag(context.Background(), "f", newTestUser("u1"))
	if err == nil {
		t.Error("expected timeout error but got nil")
	}
	if result == nil || result.Enabled {
		t.Error("expected disabled result on timeout")
	}
}

// ---------------------------------------------------------------------------
// Headers common to every request
// ---------------------------------------------------------------------------

func TestClient_RequestHeaders(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("f", true, nil)
	fb.serveAssign()
	fb.serveTrack()

	c := mustNewClient(t, fb.URL, exp.WithAPIKey("secret-key"))
	ctx := context.Background()
	user := newTestUser("user-1")
	_, _ = c.EvaluateFlag(ctx, "f", user)
	_, _ = c.GetAssignment(ctx, "checkout_flow", user)
	_ = c.Track(ctx, &exp.TrackEvent{UserID: "user-1", EventName: "click", ExperimentKey: "checkout_flow"})

	reqs := fb.Requests()
	if len(reqs) != 3 {
		t.Fatalf("expected 3 requests; got %d", len(reqs))
	}
	for _, r := range reqs {
		if got := r.Header.Get("X-API-Key"); got != "secret-key" {
			t.Errorf("%s %s: X-API-Key = %q; want 'secret-key'", r.Method, r.Path, got)
		}
		if got := r.Header.Get("Accept"); got != "application/json" {
			t.Errorf("%s %s: Accept = %q", r.Method, r.Path, got)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("%s %s: Content-Type = %q", r.Method, r.Path, got)
		}
	}
}

// ---------------------------------------------------------------------------
// Client.EvaluateFlag tests
// ---------------------------------------------------------------------------

func TestClient_EvaluateFlag_RequestAndMapping(t *testing.T) {
	fb := newFakeBackend(t)
	fb.respondJSON(http.MethodGet, "/api/v1/feature-flags/evaluate/new search/v2", http.StatusOK,
		map[string]interface{}{
			"key":     "new search/v2",
			"enabled": true,
			"config":  map[string]interface{}{"variant": "blue", "limit": 3},
		})

	c := mustNewClient(t, fb.URL)
	result, err := c.EvaluateFlag(context.Background(), "new search/v2", newTestUser("user 1&2"))
	if err != nil {
		t.Fatalf("EvaluateFlag error: %v", err)
	}

	req := fb.Requests()[0]
	if req.Method != http.MethodGet {
		t.Errorf("method = %s; want GET", req.Method)
	}
	if req.EscapedPath != "/api/v1/feature-flags/evaluate/new%20search%2Fv2" {
		t.Errorf("escaped path = %q; want key path-escaped", req.EscapedPath)
	}
	if req.Query["user_id"] != "user 1&2" {
		t.Errorf("user_id query = %q; want 'user 1&2'", req.Query["user_id"])
	}
	if len(req.RawBody) != 0 {
		t.Errorf("GET should have no body; got %s", req.RawBody)
	}

	if result.Key != "new search/v2" {
		t.Errorf("Key = %q", result.Key)
	}
	if !result.Enabled {
		t.Error("Enabled = false; want true")
	}
	if result.Config["variant"] != "blue" || result.Config["limit"] != float64(3) {
		t.Errorf("Config = %v; want variant=blue limit=3", result.Config)
	}
}

func TestClient_EvaluateFlag_Disabled(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("off-flag", false, nil)

	c := mustNewClient(t, fb.URL)
	result, err := c.EvaluateFlag(context.Background(), "off-flag", newTestUser("user-1"))
	if err != nil {
		t.Fatalf("EvaluateFlag error: %v", err)
	}
	if result.Enabled {
		t.Error("expected Enabled=false")
	}
	if result.Config != nil {
		t.Errorf("Config = %v; want nil for null config", result.Config)
	}
}

func TestClient_EvaluateFlag_NonObjectConfig(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("string-config", true, "just-a-string")

	c := mustNewClient(t, fb.URL)
	result, err := c.EvaluateFlag(context.Background(), "string-config", newTestUser("user-1"))
	if err != nil {
		t.Fatalf("EvaluateFlag must not fail on a non-object config: %v", err)
	}
	if !result.Enabled || result.Config != nil {
		t.Errorf("got enabled=%v config=%v; want enabled=true config=nil", result.Enabled, result.Config)
	}
}

func TestClient_EvaluateFlag_CacheHit(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("cached-flag", true, nil)

	c := mustNewClient(t, fb.URL)
	user := newTestUser("user-cache")
	for i := 0; i < 3; i++ {
		if _, err := c.EvaluateFlag(context.Background(), "cached-flag", user); err != nil {
			t.Fatalf("call %d EvaluateFlag error: %v", i, err)
		}
	}
	if fb.Count() != 1 {
		t.Errorf("expected 1 request (cache hits); got %d", fb.Count())
	}
}

func TestClient_EvaluateFlag_CacheIsPerUserAndKey(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("f", true, nil)
	fb.serveFlag("g", true, nil)

	c := mustNewClient(t, fb.URL)
	ctx := context.Background()
	_, _ = c.EvaluateFlag(ctx, "f", newTestUser("u1"))
	_, _ = c.EvaluateFlag(ctx, "f", newTestUser("u2"))
	_, _ = c.EvaluateFlag(ctx, "g", newTestUser("u1"))
	_, _ = c.EvaluateFlag(ctx, "f", newTestUser("u1"))

	if fb.Count() != 3 {
		t.Errorf("expected 3 requests (u1/f, u2/f, u1/g); got %d", fb.Count())
	}
}

func TestClient_EvaluateFlag_CacheTTLExpiry(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("ttl-flag", true, nil)

	c := mustNewClient(t, fb.URL, exp.WithCacheTTL(50*time.Millisecond))
	user := newTestUser("user-ttl")
	_, _ = c.EvaluateFlag(context.Background(), "ttl-flag", user)
	_, _ = c.EvaluateFlag(context.Background(), "ttl-flag", user)
	time.Sleep(100 * time.Millisecond)
	_, _ = c.EvaluateFlag(context.Background(), "ttl-flag", user)

	if fb.Count() != 2 {
		t.Errorf("expected 2 requests (second after TTL expiry); got %d", fb.Count())
	}
}

func TestClient_EvaluateFlag_NotFound(t *testing.T) {
	fb := newFakeBackend(t) // no route → 404

	c := mustNewClient(t, fb.URL)
	result, err := c.EvaluateFlag(context.Background(), "missing-flag", newTestUser("user-1"))
	if err == nil {
		t.Fatal("expected error for 404 response")
	}
	var apiErr *exp.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusNotFound {
		t.Errorf("expected *APIError 404; got %v", err)
	}
	if result == nil || result.Enabled || result.Key != "missing-flag" {
		t.Errorf("expected disabled result for the requested key; got %+v", result)
	}
}

func TestClient_EvaluateFlag_FailureNotCached(t *testing.T) {
	fb := newFakeBackend(t)
	calls := 0
	fb.handle(http.MethodGet, "/api/v1/feature-flags/evaluate/flaky", func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			http.Error(w, `{"detail":"boom"}`, http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"key": "flaky", "enabled": true})
	})

	c := mustNewClient(t, fb.URL)
	user := newTestUser("user-1")
	if _, err := c.EvaluateFlag(context.Background(), "flaky", user); err == nil {
		t.Fatal("expected first call to fail")
	}
	result, err := c.EvaluateFlag(context.Background(), "flaky", user)
	if err != nil || !result.Enabled {
		t.Errorf("second call should retry and succeed; got %+v, %v", result, err)
	}
	if fb.Count() != 2 {
		t.Errorf("expected 2 requests; got %d", fb.Count())
	}
}

func TestClient_EvaluateFlag_ContextCancelled(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(500 * time.Millisecond)
		_, _ = w.Write([]byte(`{"key":"f","enabled":true}`))
	}))
	defer srv.Close()

	c := mustNewClient(t, srv.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	if _, err := c.EvaluateFlag(ctx, "slow-flag", newTestUser("u1")); err == nil {
		t.Error("expected context cancellation error")
	}
}

func TestClient_EvaluateFlag_Validation(t *testing.T) {
	c := mustNewClient(t, "http://localhost:9")
	if _, err := c.EvaluateFlag(context.Background(), "", newTestUser("u1")); err == nil {
		t.Error("expected error for empty flagKey")
	}
	if _, err := c.EvaluateFlag(context.Background(), "flag", nil); err == nil {
		t.Error("expected error for nil user")
	}
	if _, err := c.EvaluateFlag(context.Background(), "flag", &exp.User{}); err == nil {
		t.Error("expected error for empty user ID")
	}
}

// ---------------------------------------------------------------------------
// Client.GetAssignment tests
// ---------------------------------------------------------------------------

func TestClient_GetAssignment_RequestAndMapping(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()

	c := mustNewClient(t, fb.URL)
	user := newTestUserWithAttrs("user-1", map[string]interface{}{"plan": "pro", "country": "US"})
	got, err := c.GetAssignment(context.Background(), "checkout_flow", user)
	if err != nil {
		t.Fatalf("GetAssignment error: %v", err)
	}

	req := fb.Requests()[0]
	if req.Method != http.MethodPost || req.Path != "/api/v1/tracking/assign" {
		t.Errorf("request = %s %s; want POST /api/v1/tracking/assign", req.Method, req.Path)
	}
	if req.Body["experiment_key"] != "checkout_flow" || req.Body["user_id"] != "user-1" {
		t.Errorf("body = %v", req.Body)
	}
	ctxAttrs, _ := req.Body["context"].(map[string]interface{})
	if ctxAttrs["plan"] != "pro" || ctxAttrs["country"] != "US" {
		t.Errorf("context = %v; want user attributes", req.Body["context"])
	}
	if _, has := req.Body["attributes"]; has {
		t.Error("body must not use the legacy 'attributes' field")
	}

	if got.ExperimentKey != "checkout_flow" || got.UserID != "user-1" {
		t.Errorf("ExperimentKey/UserID = %q/%q", got.ExperimentKey, got.UserID)
	}
	if got.VariantID != "8b6f1c2e-0000-4000-8000-000000000001" || got.VariantName != "treatment" {
		t.Errorf("VariantID/VariantName = %q/%q", got.VariantID, got.VariantName)
	}
	if got.IsControl {
		t.Error("IsControl = true; want false")
	}
	if got.Configuration["headline"] != "Buy now" || got.Configuration["discount"] != float64(10) {
		t.Errorf("Configuration = %v", got.Configuration)
	}
}

func TestClient_GetAssignment_OmitsContextWithoutAttributes(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()

	c := mustNewClient(t, fb.URL)
	if _, err := c.GetAssignment(context.Background(), "checkout_flow", newTestUser("user-1")); err != nil {
		t.Fatalf("GetAssignment error: %v", err)
	}
	if _, has := fb.Requests()[0].Body["context"]; has {
		t.Errorf("context should be omitted when the user has no attributes; body = %s", fb.Requests()[0].RawBody)
	}
}

func TestClient_GetAssignment_Sticky_CacheHit(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()

	c := mustNewClient(t, fb.URL)
	user := newTestUser("user-1")
	first, err := c.GetAssignment(context.Background(), "checkout_flow", user)
	if err != nil {
		t.Fatalf("GetAssignment error: %v", err)
	}
	second, err := c.GetAssignment(context.Background(), "checkout_flow", user)
	if err != nil {
		t.Fatalf("GetAssignment error: %v", err)
	}
	if first.VariantName != second.VariantName || first.VariantID != second.VariantID {
		t.Errorf("assignment changed between calls: %+v vs %+v", first, second)
	}
	if fb.Count() != 1 {
		t.Errorf("expected 1 request (sticky cache hit); got %d", fb.Count())
	}
}

func TestClient_GetAssignment_NotFound(t *testing.T) {
	fb := newFakeBackend(t)
	fb.handle(http.MethodPost, "/api/v1/tracking/assign", func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"Active experiment with key 'nope' not found"}`, http.StatusNotFound)
	})

	c := mustNewClient(t, fb.URL)
	got, err := c.GetAssignment(context.Background(), "nope", newTestUser("u1"))
	if err == nil {
		t.Fatal("expected error for 404 response")
	}
	if got != nil {
		t.Errorf("expected nil assignment on failure; got %+v", got)
	}
	var apiErr *exp.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusNotFound {
		t.Errorf("expected *APIError 404; got %v", err)
	}

	// Failures are never cached: the next call hits the server again.
	_, _ = c.GetAssignment(context.Background(), "nope", newTestUser("u1"))
	if fb.Count() != 2 {
		t.Errorf("expected 2 requests; got %d", fb.Count())
	}
}

func TestClient_GetAssignment_Validation(t *testing.T) {
	c := mustNewClient(t, "http://localhost:9")
	if _, err := c.GetAssignment(context.Background(), "", newTestUser("u1")); err == nil {
		t.Error("expected error for empty experimentKey")
	}
	if _, err := c.GetAssignment(context.Background(), "exp", nil); err == nil {
		t.Error("expected error for nil user")
	}
}

// ---------------------------------------------------------------------------
// Client.Track tests
// ---------------------------------------------------------------------------

func TestClient_Track_WithExperimentKey(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)
	ts := time.Date(2026, 9, 11, 10, 30, 0, 0, time.UTC)
	err := c.Track(context.Background(), &exp.TrackEvent{
		UserID:        "user-track",
		EventName:     "purchase",
		ExperimentKey: "checkout_flow",
		Value:         exp.Float64(12.5),
		Properties:    map[string]interface{}{"sku": "pro"},
		Timestamp:     ts,
	})
	if err != nil {
		t.Fatalf("Track error: %v", err)
	}

	reqs := fb.Requests()
	if len(reqs) != 1 {
		t.Fatalf("expected 1 request; got %d", len(reqs))
	}
	req := reqs[0]
	if req.Method != http.MethodPost || req.Path != "/api/v1/tracking/track" {
		t.Errorf("request = %s %s; want POST /api/v1/tracking/track", req.Method, req.Path)
	}
	want := map[string]interface{}{
		"event_type":     "purchase",
		"event_name":     "purchase",
		"user_id":        "user-track",
		"experiment_key": "checkout_flow",
		"value":          12.5,
		"timestamp":      "2026-09-11T10:30:00Z",
	}
	for k, v := range want {
		if req.Body[k] != v {
			t.Errorf("body[%q] = %v; want %v", k, req.Body[k], v)
		}
	}
	meta, _ := req.Body["metadata"].(map[string]interface{})
	if meta["sku"] != "pro" {
		t.Errorf("metadata = %v; want sku=pro", req.Body["metadata"])
	}
	if _, has := req.Body["feature_flag_key"]; has {
		t.Error("feature_flag_key must be omitted when empty")
	}
	if _, has := req.Body["properties"]; has {
		t.Error("legacy 'properties' field must not be sent")
	}
}

func TestClient_Track_WithFeatureFlagKeyAndEventType(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)
	err := c.Track(context.Background(), &exp.TrackEvent{
		UserID:         "u1",
		EventName:      "search",
		EventType:      "interaction",
		FeatureFlagKey: "new_search",
	})
	if err != nil {
		t.Fatalf("Track error: %v", err)
	}
	body := fb.Requests()[0].Body
	if body["event_type"] != "interaction" || body["event_name"] != "search" {
		t.Errorf("event_type/event_name = %v/%v", body["event_type"], body["event_name"])
	}
	if body["feature_flag_key"] != "new_search" {
		t.Errorf("feature_flag_key = %v", body["feature_flag_key"])
	}
	for _, absent := range []string{"experiment_key", "value", "metadata", "timestamp"} {
		if _, has := body[absent]; has {
			t.Errorf("%s must be omitted when unset", absent)
		}
	}
}

func TestClient_Track_WithoutKey_FansOutToCachedEntries(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()
	fb.serveFlag("new_search", true, nil)
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)
	ctx := context.Background()
	user := newTestUser("user-1")
	if _, err := c.GetAssignment(ctx, "checkout_flow", user); err != nil {
		t.Fatal(err)
	}
	if _, err := c.EvaluateFlag(ctx, "new_search", user); err != nil {
		t.Fatal(err)
	}
	// Another user's cache entries must not leak into this user's fan-out.
	if _, err := c.EvaluateFlag(ctx, "new_search", newTestUser("user-2")); err != nil {
		t.Fatal(err)
	}

	err := c.Track(ctx, &exp.TrackEvent{
		UserID:     "user-1",
		EventName:  "page_view",
		Value:      exp.Float64(1),
		Properties: map[string]interface{}{"page": "/home"},
	})
	if err != nil {
		t.Fatalf("Track error: %v", err)
	}

	reqs := fb.Requests()
	last := reqs[len(reqs)-1]
	if last.Method != http.MethodPost || last.Path != "/api/v1/tracking/batch" {
		t.Fatalf("last request = %s %s; want POST /api/v1/tracking/batch", last.Method, last.Path)
	}
	events, _ := last.Body["events"].([]interface{})
	if len(events) != 2 {
		t.Fatalf("expected 2 fan-out entries; got %d: %s", len(events), last.RawBody)
	}
	first, _ := events[0].(map[string]interface{})
	second, _ := events[1].(map[string]interface{})
	if first["experiment_key"] != "checkout_flow" {
		t.Errorf("entry 0 = %v; want experiment_key=checkout_flow", first)
	}
	if second["feature_flag_key"] != "new_search" {
		t.Errorf("entry 1 = %v; want feature_flag_key=new_search", second)
	}
	for i, e := range []map[string]interface{}{first, second} {
		if e["event_type"] != "page_view" || e["event_name"] != "page_view" || e["user_id"] != "user-1" || e["value"] != float64(1) {
			t.Errorf("entry %d missing base fields: %v", i, e)
		}
		meta, _ := e["metadata"].(map[string]interface{})
		if meta["page"] != "/home" {
			t.Errorf("entry %d metadata = %v", i, e["metadata"])
		}
	}
}

func TestClient_Track_WithoutKey_NothingCached_SendsNothing(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)
	if err := c.Track(context.Background(), &exp.TrackEvent{UserID: "nobody", EventName: "page_view"}); err != nil {
		t.Fatalf("Track error: %v", err)
	}
	if fb.Count() != 0 {
		t.Errorf("expected no request when nothing is cached; got %d", fb.Count())
	}
}

func TestClient_Track_ServerError_ReturnsErrorNoPanic(t *testing.T) {
	fb := newFakeBackend(t)
	fb.handle(http.MethodPost, "/api/v1/tracking/track", func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"server error"}`, http.StatusInternalServerError)
	})

	c := mustNewClient(t, fb.URL)
	err := c.Track(context.Background(), &exp.TrackEvent{UserID: "u1", EventName: "click", ExperimentKey: "e"})
	if err == nil {
		t.Error("expected error for 500 response")
	}
}

func TestClient_Track_NeverPanics(t *testing.T) {
	c := mustNewClient(t, "http://127.0.0.1:1", exp.WithTimeout(200*time.Millisecond))
	ctx := context.Background()

	cases := []*exp.TrackEvent{
		nil,
		{},
		{UserID: "u1"},
		{EventName: "click"},
		{UserID: "u1", EventName: "click", ExperimentKey: "e"}, // unreachable server
		{UserID: "u1", EventName: "click"},                     // nothing cached
	}
	for i, ev := range cases {
		func() {
			defer func() {
				if r := recover(); r != nil {
					t.Errorf("case %d panicked: %v", i, r)
				}
			}()
			_ = c.Track(ctx, ev)
		}()
	}
	if err := c.TrackBatch(ctx, []*exp.TrackEvent{{UserID: "u1", EventName: "x"}, nil}); err == nil {
		t.Error("expected validation error for nil event in batch")
	}
}

// ---------------------------------------------------------------------------
// Client.TrackBatch tests
// ---------------------------------------------------------------------------

func TestClient_TrackBatch_ChunksAt100(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)
	events := make([]*exp.TrackEvent, 0, 150)
	for i := 0; i < 150; i++ {
		events = append(events, &exp.TrackEvent{UserID: "u" + intToStr(i), EventName: "click", ExperimentKey: "e"})
	}
	if err := c.TrackBatch(context.Background(), events); err != nil {
		t.Fatalf("TrackBatch error: %v", err)
	}

	reqs := fb.Requests()
	if len(reqs) != 2 {
		t.Fatalf("expected 2 batch requests; got %d", len(reqs))
	}
	sizes := []int{}
	for _, r := range reqs {
		if r.Path != "/api/v1/tracking/batch" {
			t.Errorf("path = %q", r.Path)
		}
		evs, _ := r.Body["events"].([]interface{})
		sizes = append(sizes, len(evs))
	}
	if sizes[0] != 100 || sizes[1] != 50 {
		t.Errorf("chunk sizes = %v; want [100 50]", sizes)
	}
}

func TestClient_TrackBatch_ExpandsUnkeyedEvents(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)
	ctx := context.Background()
	if _, err := c.GetAssignment(ctx, "checkout_flow", newTestUser("user-1")); err != nil {
		t.Fatal(err)
	}

	err := c.TrackBatch(ctx, []*exp.TrackEvent{
		{UserID: "user-1", EventName: "purchase", FeatureFlagKey: "f"},
		{UserID: "user-1", EventName: "page_view"}, // fans out to checkout_flow
		{UserID: "user-9", EventName: "page_view"}, // nothing cached → dropped
	})
	if err != nil {
		t.Fatalf("TrackBatch error: %v", err)
	}
	reqs := fb.Requests()
	last := reqs[len(reqs)-1]
	evs, _ := last.Body["events"].([]interface{})
	if len(evs) != 2 {
		t.Fatalf("expected 2 entries; got %s", last.RawBody)
	}
	e0, _ := evs[0].(map[string]interface{})
	e1, _ := evs[1].(map[string]interface{})
	if e0["feature_flag_key"] != "f" || e1["experiment_key"] != "checkout_flow" || e1["event_name"] != "page_view" {
		t.Errorf("entries = %v, %v", e0, e1)
	}
}

func TestClient_TrackBatch_Empty_SendsNothing(t *testing.T) {
	fb := newFakeBackend(t)
	c := mustNewClient(t, fb.URL)
	if err := c.TrackBatch(context.Background(), nil); err != nil {
		t.Fatalf("TrackBatch(nil) error: %v", err)
	}
	if err := c.TrackBatch(context.Background(), []*exp.TrackEvent{}); err != nil {
		t.Fatalf("TrackBatch(empty) error: %v", err)
	}
	if fb.Count() != 0 {
		t.Errorf("expected no request; got %d", fb.Count())
	}
}

// ---------------------------------------------------------------------------
// Client.Close tests
// ---------------------------------------------------------------------------

func TestClient_Close(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("f", true, nil)

	c, err := exp.New(exp.WithBaseURL(fb.URL))
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	_, _ = c.EvaluateFlag(context.Background(), "f", newTestUser("u1"))

	if err := c.Close(); err != nil {
		t.Errorf("Close() error: %v", err)
	}
	if err := c.Close(); err != nil {
		t.Errorf("second Close() error: %v", err)
	}

	// The cache is cleared on Close, so a further call goes to the server.
	_, _ = c.EvaluateFlag(context.Background(), "f", newTestUser("u1"))
	if fb.Count() != 2 {
		t.Errorf("expected cache cleared on Close; requests = %d", fb.Count())
	}
}

// ---------------------------------------------------------------------------
// Concurrency / race-safety tests
// ---------------------------------------------------------------------------

func TestClient_Concurrent(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveFlag("race-flag", true, nil)
	fb.serveAssign()
	fb.serveTrack()

	c := mustNewClient(t, fb.URL)

	var wg sync.WaitGroup
	const goroutines = 20
	const opsPerG = 25

	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func(gid int) {
			defer wg.Done()
			ctx := context.Background()
			for j := 0; j < opsPerG; j++ {
				user := newTestUser("cuser-" + intToStr((gid*opsPerG+j)%7))
				_, _ = c.EvaluateFlag(ctx, "race-flag", user)
				_, _ = c.GetAssignment(ctx, "checkout_flow", user)
				_ = c.Track(ctx, &exp.TrackEvent{UserID: user.ID, EventName: "page_view"})
				_ = c.Track(ctx, &exp.TrackEvent{UserID: user.ID, EventName: "purchase", ExperimentKey: "checkout_flow"})
			}
		}(i)
	}
	wg.Wait()
}

// ---------------------------------------------------------------------------
// HTTPClient tests
// ---------------------------------------------------------------------------

func TestHTTPClient_Get_Success(t *testing.T) {
	type response struct {
		Value string `json:"value"`
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(response{Value: "hello"})
	}))
	defer srv.Close()

	h := exp.NewHTTPClient(srv.URL, "key", 5*time.Second)
	var result response
	if err := h.Get(context.Background(), "/test", &result); err != nil {
		t.Fatalf("Get error: %v", err)
	}
	if result.Value != "hello" {
		t.Errorf("result.Value = %q; want 'hello'", result.Value)
	}
}

func TestHTTPClient_Get_ServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "7")
		http.Error(w, "rate limited", http.StatusTooManyRequests)
	}))
	defer srv.Close()

	h := exp.NewHTTPClient(srv.URL, "key", 5*time.Second)
	err := h.Get(context.Background(), "/fail", nil)
	var apiErr *exp.APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("expected *APIError; got %v", err)
	}
	if apiErr.StatusCode != http.StatusTooManyRequests || apiErr.RetryAfter != "7" {
		t.Errorf("APIError = %+v", apiErr)
	}
	if !strings.Contains(apiErr.Error(), "429") {
		t.Errorf("Error() = %q; want status in message", apiErr.Error())
	}
}

func TestHTTPClient_Post_Success(t *testing.T) {
	type payload struct {
		Name string `json:"name"`
	}
	type response struct {
		Echo string `json:"echo"`
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var p payload
		_ = json.NewDecoder(r.Body).Decode(&p)
		_ = json.NewEncoder(w).Encode(response{Echo: "got:" + p.Name})
	}))
	defer srv.Close()

	h := exp.NewHTTPClient(srv.URL, "", 5*time.Second)
	var result response
	if err := h.Post(context.Background(), "/echo", payload{Name: "world"}, &result); err != nil {
		t.Fatalf("Post error: %v", err)
	}
	if result.Echo != "got:world" {
		t.Errorf("result.Echo = %q; want 'got:world'", result.Echo)
	}
}

func TestHTTPClient_ContextCancellation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(300 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	h := exp.NewHTTPClient(srv.URL, "key", 5*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	if err := h.Get(ctx, "/slow", nil); err == nil {
		t.Error("expected context deadline error")
	}
}

func TestHTTPClient_Headers(t *testing.T) {
	var got http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		_, _ = w.Write([]byte("{}"))
	}))
	defer srv.Close()

	h := exp.NewHTTPClient(srv.URL, "my-secret", 5*time.Second)
	_ = h.Get(context.Background(), "/auth-test", nil)

	if got.Get("X-API-Key") != "my-secret" {
		t.Errorf("X-API-Key = %q; want 'my-secret'", got.Get("X-API-Key"))
	}
	if got.Get("Accept") != "application/json" || got.Get("Content-Type") != "application/json" {
		t.Errorf("Accept/Content-Type = %q/%q", got.Get("Accept"), got.Get("Content-Type"))
	}

	h = exp.NewHTTPClient(srv.URL, "", 5*time.Second)
	_ = h.Get(context.Background(), "/no-key", nil)
	if got.Get("X-API-Key") != "" {
		t.Errorf("expected no X-API-Key header; got %q", got.Get("X-API-Key"))
	}
}
