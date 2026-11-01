package experimentation_test

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strings"
	"testing"

	exp "github.com/controlvar/controlvar/sdk/go"
)

// EvaluateFlag sends User.Attributes as context (#1497): the query parameter
// the JS and Python SDKs send, nothing when there are no attributes, and no
// request at all for attributes that cannot be encoded as JSON.

const contextFlagPath = "/api/v1/feature-flags/evaluate/checkout-label"

// requestLine is the method, path and query of a recorded request, as sent.
func requestLine(req recordedRequest) string {
	line := req.Method + " " + req.EscapedPath
	if req.RawQuery != "" {
		line += "?" + req.RawQuery
	}
	return line
}

// wrapsJSONError reports whether err wraps the error encoding/json gives for
// a value (NaN, an infinity) or a type (a channel, a func) it cannot encode.
func wrapsJSONError(err error) bool {
	var badValue *json.UnsupportedValueError
	var badType *json.UnsupportedTypeError
	return errors.As(err, &badValue) || errors.As(err, &badType)
}

// ---------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------

// Nil or empty attributes send no context parameter: the URL is the one an
// SDK without this change sends.
func TestEvaluateFlag_WithoutAttributes_URLIsUnchanged(t *testing.T) {
	for name, attrs := range map[string]map[string]interface{}{
		"nil":   nil,
		"empty": {},
	} {
		t.Run(name, func(t *testing.T) {
			fb := newFakeBackend(t)
			fb.serveValueFlag("checkout-label", true, "string", "Complete purchase")
			c := mustNewClient(t, fb.URL)

			if _, err := c.EvaluateFlag(context.Background(), "checkout-label", newTestUserWithAttrs("user-1", attrs)); err != nil {
				t.Fatalf("EvaluateFlag error: %v", err)
			}
			reqs := fb.Requests()
			if len(reqs) != 1 {
				t.Fatalf("requests = %d; want 1", len(reqs))
			}
			if got, want := requestLine(reqs[0]), "GET "+contextFlagPath+"?user_id=user-1"; got != want {
				t.Errorf("request = %q; want %q", got, want)
			}
			if len(reqs[0].RawBody) != 0 {
				t.Errorf("GET should have no body; got %s", reqs[0].RawBody)
			}
		})
	}
}

// The attributes travel as context=<URL-encoded compact JSON>, after user_id,
// and reach the server as the same JSON text.
func TestEvaluateFlag_SendsAttributesAsCompactJSONContext(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("checkout-label", true, "string", "Complete purchase")
	c := mustNewClient(t, fb.URL)

	user := newTestUserWithAttrs("user-1", map[string]interface{}{
		"plan":    "enterprise",
		"country": "US",
		// A space, "+", "#" and "%" would each change or cut the query string
		// if the JSON were not URL-encoded.
		"company": "A+B Co #1, 100%",
		"seats":   12,
		"beta":    true,
		"app":     map[string]interface{}{"version": "3.2.1"},
		"tags":    []string{"a", "b"},
	})
	if _, err := c.EvaluateFlag(context.Background(), "checkout-label", user); err != nil {
		t.Fatalf("EvaluateFlag error: %v", err)
	}

	reqs := fb.Requests()
	if len(reqs) != 1 {
		t.Fatalf("requests = %d; want 1", len(reqs))
	}
	req := reqs[0]
	want := `{"app":{"version":"3.2.1"},"beta":true,"company":"A+B Co #1, 100%","country":"US","plan":"enterprise","seats":12,"tags":["a","b"]}`
	if got := req.Query["context"]; got != want {
		t.Errorf("context = %q; want the attributes as compact JSON %q (request %q)", got, want, requestLine(req))
	}
	if req.Query["user_id"] != "user-1" || len(req.Query) != 2 {
		t.Errorf("query = %v; want user_id and context only", req.Query)
	}
	if !strings.HasPrefix(req.RawQuery, "user_id=user-1&context=") {
		t.Errorf("raw query = %q; want user_id=user-1&context=...", req.RawQuery)
	}
	if len(req.RawBody) != 0 {
		t.Errorf("GET should have no body; got %s", req.RawBody)
	}
}

// ---------------------------------------------------------------------------
// Failures: nothing cached, a disabled result and the error
// ---------------------------------------------------------------------------

// Attributes encoding/json cannot encode send nothing and cache nothing; the
// caller gets the disabled result and an error wrapping the encoding error.
func TestEvaluateFlag_UnencodableAttributes_SendAndCacheNothing(t *testing.T) {
	for name, bad := range map[string]interface{}{
		"NaN":      math.NaN(),
		"infinity": math.Inf(1),
		"channel":  make(chan int),
	} {
		t.Run(name, func(t *testing.T) {
			fb := newFakeBackend(t)
			fb.serveValueFlag("checkout-label", true, "string", "Complete purchase")
			c := mustNewClient(t, fb.URL)
			attrs := map[string]interface{}{"plan": "enterprise", "score": bad}
			_, marshalErr := json.Marshal(attrs)
			if marshalErr == nil {
				t.Fatal("the test's attributes must not encode")
			}

			result, err := c.EvaluateFlag(context.Background(), "checkout-label", newTestUserWithAttrs("user-1", attrs))
			if err == nil {
				t.Fatal("EvaluateFlag error = nil; want the encoding error")
			}
			if !wrapsJSONError(err) || !strings.Contains(err.Error(), marshalErr.Error()) {
				t.Errorf("error = %q; want it to wrap %q", err, marshalErr)
			}
			if result == nil || result.Enabled || result.Key != "checkout-label" || result.Value != nil || result.ValueType != "" {
				t.Errorf("result = %+v; want the disabled result for the key", result)
			}
			if got := result.StringValue("Buy now"); got != "Buy now" {
				t.Errorf("StringValue = %q; want the default", got)
			}
			if fb.Count() != 0 {
				t.Fatalf("requests = %d; want 0 (nothing is sent)", fb.Count())
			}

			// Nothing was cached: the same user and flag with encodable
			// attributes asks the server.
			again, err := c.EvaluateFlag(context.Background(), "checkout-label", newTestUserWithAttrs("user-1", map[string]interface{}{"plan": "enterprise"}))
			if err != nil || !again.Enabled {
				t.Fatalf("second call = %+v, %v; want the server's answer", again, err)
			}
			if fb.Count() != 1 {
				t.Errorf("requests after the second call = %d; want 1", fb.Count())
			}
		})
	}
}

// A context the server refuses (its 422 over the targeting context limits,
// or a proxy's 400 or 414 for a long URL) is an error with the disabled
// result, like any other non-2xx, and is not cached. The SDK keeps no copy
// of the limits: it sends a context larger than 8,192 bytes and lets the
// server answer.
func TestEvaluateFlag_ContextTheServerRefuses_IsAnErrorAndNotCached(t *testing.T) {
	for _, status := range []int{http.StatusUnprocessableEntity, http.StatusBadRequest, http.StatusRequestURITooLong} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			fb := newFakeBackend(t)
			fb.respondJSON(http.MethodGet, contextFlagPath, status,
				map[string]interface{}{"detail": "Targeting context larger than 8192 bytes"})
			c := mustNewClient(t, fb.URL)
			user := newTestUserWithAttrs("user-1", map[string]interface{}{"notes": strings.Repeat("x", 9000)})

			for call := 1; call <= 2; call++ {
				result, err := c.EvaluateFlag(context.Background(), "checkout-label", user)
				var apiErr *exp.APIError
				if !errors.As(err, &apiErr) || apiErr.StatusCode != status {
					t.Fatalf("call %d: error = %v; want *APIError %d", call, err, status)
				}
				if result == nil || result.Enabled || result.Key != "checkout-label" || result.Value != nil {
					t.Errorf("call %d: result = %+v; want the disabled result for the key", call, result)
				}
				if fb.Count() != call {
					t.Fatalf("after call %d, requests = %d; want %d (a refusal is not cached)", call, fb.Count(), call)
				}
			}
			if got := len(fb.Requests()[0].Query["context"]); got <= 8192 {
				t.Errorf("context sent = %d bytes; want the whole context, over 8192", got)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Server tolerance (EMP-8): a server without flag values
// ---------------------------------------------------------------------------

// 0.39.0's evaluate route reads context and answers without value or
// value_type; a server that ignores context answers from the rollout. Either
// answer is decoded with no error, and the value accessors return their
// defaults.
func TestEvaluateFlag_WithContext_ServerWithoutFlagValuesIsDecoded(t *testing.T) {
	servers := map[string]http.HandlerFunc{
		// A rule on plan = enterprise, as 0.39.0 answers it.
		"0.39.0": func(w http.ResponseWriter, r *http.Request) {
			var ctx map[string]interface{}
			if err := json.Unmarshal([]byte(r.URL.Query().Get("context")), &ctx); err != nil {
				w.WriteHeader(http.StatusUnprocessableEntity)
				_ = json.NewEncoder(w).Encode(map[string]interface{}{"detail": "Query parameter 'context' must be a URL-encoded JSON object"})
				return
			}
			enabled, reason := false, "rollout"
			if ctx["plan"] == "enterprise" {
				enabled, reason = true, "targeting_rule"
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"key": "checkout-label", "enabled": enabled, "config": nil, "reason": reason})
		},
		// A server that does not read context: the rollout answers.
		"ignores-context": func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"key": "checkout-label", "enabled": true, "config": nil})
		},
	}
	for name, handler := range servers {
		t.Run(name, func(t *testing.T) {
			fb := newFakeBackend(t)
			fb.handle(http.MethodGet, contextFlagPath, handler)
			c := mustNewClient(t, fb.URL)

			result, err := c.EvaluateFlag(context.Background(), "checkout-label", newTestUserWithAttrs("user-1", map[string]interface{}{"plan": "enterprise"}))
			if err != nil {
				t.Fatalf("EvaluateFlag error: %v", err)
			}
			if !result.Enabled || result.Key != "checkout-label" {
				t.Errorf("result = %+v; want the server's enabled answer for the key", result)
			}
			if result.Value != nil || result.ValueType != "" {
				t.Errorf("Value = %s, ValueType = %q; want none", result.Value, result.ValueType)
			}
			if got := result.StringValue("Buy now"); got != "Buy now" {
				t.Errorf("StringValue = %q; want the default", got)
			}
			if got := result.NumberValue(2.5); got != 2.5 {
				t.Errorf("NumberValue = %v; want the default", got)
			}
			b := defaultBanner
			if err := result.DecodeValue(&b); err == nil || b != defaultBanner {
				t.Errorf("DecodeValue = %v, v = %+v; want its error and v untouched", err, b)
			}
			if got := fb.Requests()[0].Query["context"]; got != `{"plan":"enterprise"}` {
				t.Errorf("context = %q; want the user's attributes", got)
			}

			// The answer follows the attributes where the server reads them.
			other, err := c.EvaluateFlag(context.Background(), "checkout-label", newTestUserWithAttrs("user-2", map[string]interface{}{"plan": "free"}))
			if err != nil {
				t.Fatalf("EvaluateFlag error for user-2: %v", err)
			}
			if wantEnabled := name != "0.39.0"; other.Enabled != wantEnabled {
				t.Errorf("user-2 Enabled = %v; want %v", other.Enabled, wantEnabled)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// The cache key is the user and the key, not the attributes
// ---------------------------------------------------------------------------

func TestEvaluateFlag_CacheKeyIgnoresAttributes(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("checkout-label", true, "string", "Complete purchase")
	c := mustNewClient(t, fb.URL)
	ctx := context.Background()

	if _, err := c.EvaluateFlag(ctx, "checkout-label", newTestUserWithAttrs("user-1", map[string]interface{}{"plan": "enterprise"})); err != nil {
		t.Fatalf("EvaluateFlag error: %v", err)
	}
	// Within the TTL, the same user and flag get the cached answer whatever
	// the attributes, unencodable ones included: nothing is sent or encoded.
	for _, attrs := range []map[string]interface{}{
		{"plan": "free"},
		nil,
		{"score": math.NaN()},
	} {
		result, err := c.EvaluateFlag(ctx, "checkout-label", newTestUserWithAttrs("user-1", attrs))
		if err != nil || !result.Enabled || result.StringValue("Buy now") != "Complete purchase" {
			t.Errorf("attributes %v: result = %+v, %v; want the cached answer", attrs, result, err)
		}
	}
	if fb.Count() != 1 {
		t.Fatalf("requests = %d; want 1 (the rest are cache hits)", fb.Count())
	}
	if got := fb.Requests()[0].Query["context"]; got != `{"plan":"enterprise"}` {
		t.Errorf("context = %q; want the first call's attributes", got)
	}

	// Another user is another entry.
	if _, err := c.EvaluateFlag(ctx, "checkout-label", newTestUserWithAttrs("user-2", map[string]interface{}{"plan": "enterprise"})); err != nil {
		t.Fatalf("EvaluateFlag error: %v", err)
	}
	if fb.Count() != 2 {
		t.Errorf("requests = %d; want 2", fb.Count())
	}
}

func TestClient_GetAssignment_CacheKeyIgnoresAttributes(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()
	c := mustNewClient(t, fb.URL)
	ctx := context.Background()

	for _, attrs := range []map[string]interface{}{{"plan": "enterprise"}, {"plan": "free"}, nil, {"score": math.NaN()}} {
		if _, err := c.GetAssignment(ctx, "checkout_flow", newTestUserWithAttrs("user-1", attrs)); err != nil {
			t.Fatalf("attributes %v: GetAssignment error: %v", attrs, err)
		}
	}
	if fb.Count() != 1 {
		t.Errorf("requests = %d; want 1 (the rest are cache hits)", fb.Count())
	}
}

// GetAssignment fails the same way on attributes it cannot encode: no
// request, nothing cached, the encoding error wrapped.
func TestClient_GetAssignment_UnencodableAttributes_SendAndCacheNothing(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveAssign()
	c := mustNewClient(t, fb.URL)

	got, err := c.GetAssignment(context.Background(), "checkout_flow", newTestUserWithAttrs("user-1", map[string]interface{}{"score": math.NaN()}))
	if got != nil || err == nil || !wrapsJSONError(err) {
		t.Errorf("GetAssignment = %+v, %v; want nil and the encoding error", got, err)
	}
	if fb.Count() != 0 {
		t.Fatalf("requests = %d; want 0", fb.Count())
	}
	if _, err := c.GetAssignment(context.Background(), "checkout_flow", newTestUser("user-1")); err != nil {
		t.Fatalf("GetAssignment error: %v", err)
	}
	if fb.Count() != 1 {
		t.Errorf("requests = %d; want 1 (nothing was cached)", fb.Count())
	}
}
