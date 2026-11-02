package experimentation_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"testing"

	exp "github.com/controlvar/controlvar/sdk/go"
)

// Typed flag values on EvalResult (#1497): Value, ValueType, StringValue,
// NumberValue and DecodeValue. The request, with its context, is in
// context_test.go.

// serveValueFlag answers the evaluate route as a server with flag values does:
// value, value_type, a null config and a reason.
func (fb *fakeBackend) serveValueFlag(key string, enabled bool, valueType string, value interface{}) {
	fb.respondJSON(http.MethodGet, "/api/v1/feature-flags/evaluate/"+key, http.StatusOK,
		map[string]interface{}{
			"key":        key,
			"enabled":    enabled,
			"value":      value,
			"value_type": valueType,
			"config":     nil,
			"reason":     "rollout",
		})
}

func mustEvaluate(t *testing.T, c exp.Client, key string) *exp.EvalResult {
	t.Helper()
	result, err := c.EvaluateFlag(context.Background(), key, newTestUser("user-1"))
	if err != nil {
		t.Fatalf("EvaluateFlag(%q) error: %v", key, err)
	}
	if result == nil {
		t.Fatalf("EvaluateFlag(%q) returned a nil result", key)
	}
	return result
}

// banner is what a json flag's value is decoded into in these tests.
type banner struct {
	Title string `json:"title"`
	Limit int    `json:"limit"`
}

var defaultBanner = banner{Title: "Default", Limit: 1}

// ---------------------------------------------------------------------------
// Each value type is read by its accessor; the other accessors return the default
// ---------------------------------------------------------------------------

func TestEvalResult_StringValue_ReadsAStringFlag(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("checkout-label", true, "string", "Complete purchase")
	fb.serveValueFlag("checkout-label-off", false, "string", "Buy now")
	c := mustNewClient(t, fb.URL)

	on := mustEvaluate(t, c, "checkout-label")
	if on.ValueType != "string" || string(on.Value) != `"Complete purchase"` {
		t.Fatalf("ValueType/Value = %q/%s; want string/\"Complete purchase\"", on.ValueType, on.Value)
	}
	if got := on.StringValue("fallback"); got != "Complete purchase" {
		t.Errorf("StringValue = %q; want the on value %q", got, "Complete purchase")
	}

	// A user the flag is off for gets the off value, not the caller's default.
	off := mustEvaluate(t, c, "checkout-label-off")
	if off.Enabled {
		t.Fatal("Enabled = true; want false")
	}
	if got := off.StringValue("fallback"); got != "Buy now" {
		t.Errorf("StringValue = %q; want the off value %q", got, "Buy now")
	}

	if got := on.NumberValue(7); got != 7 {
		t.Errorf("NumberValue on a string flag = %v; want the default 7", got)
	}
	b := defaultBanner
	if err := on.DecodeValue(&b); err == nil {
		t.Error("DecodeValue on a string flag returned nil; want an error")
	}
	if b != defaultBanner {
		t.Errorf("DecodeValue on a string flag changed v to %+v", b)
	}
}

func TestEvalResult_NumberValue_ReadsANumberFlag(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("discount", true, "number", 10.5)
	fb.serveValueFlag("max-items", true, "number", 10)
	fb.serveValueFlag("min-safe", true, "number", -9007199254740991)
	c := mustNewClient(t, fb.URL)

	for key, want := range map[string]float64{
		"discount":  10.5,
		"max-items": 10,
		"min-safe":  -9007199254740991,
	} {
		result := mustEvaluate(t, c, key)
		if result.ValueType != "number" {
			t.Errorf("%s: ValueType = %q; want number", key, result.ValueType)
		}
		if got := result.NumberValue(-1); got != want {
			t.Errorf("%s: NumberValue = %v; want %v", key, got, want)
		}
		if got := result.StringValue("fallback"); got != "fallback" {
			t.Errorf("%s: StringValue on a number flag = %q; want the default", key, got)
		}
		var v any = "untouched"
		if err := result.DecodeValue(&v); err == nil || v != "untouched" {
			t.Errorf("%s: DecodeValue on a number flag = %v, v = %v; want an error and v untouched", key, err, v)
		}
	}
}

func TestEvalResult_DecodeValue_ReadsAJSONFlag(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("hero-banner", true, "json", map[string]interface{}{"title": "Spring sale", "limit": 3})
	fb.serveValueFlag("tiers", true, "json", []interface{}{1, 2, 3})
	c := mustNewClient(t, fb.URL)

	hero := mustEvaluate(t, c, "hero-banner")
	if hero.ValueType != "json" {
		t.Fatalf("ValueType = %q; want json", hero.ValueType)
	}
	b := defaultBanner
	if err := hero.DecodeValue(&b); err != nil {
		t.Fatalf("DecodeValue into a struct: %v", err)
	}
	if b != (banner{Title: "Spring sale", Limit: 3}) {
		t.Errorf("DecodeValue into a struct = %+v", b)
	}
	var m map[string]any
	if err := hero.DecodeValue(&m); err != nil || m["title"] != "Spring sale" || m["limit"] != float64(3) {
		t.Errorf("DecodeValue into a map = %v, %v", m, err)
	}

	// The value replaces what v held: a field it does not name is zero.
	var partial struct {
		Title string `json:"title"`
		Extra string `json:"extra"`
	}
	partial.Extra = "set before"
	if err := hero.DecodeValue(&partial); err != nil || partial.Title != "Spring sale" || partial.Extra != "" {
		t.Errorf("DecodeValue = %+v, %v; want Title set and Extra zero", partial, err)
	}

	tiers := mustEvaluate(t, c, "tiers")
	var list []int
	if err := tiers.DecodeValue(&list); err != nil || len(list) != 3 || list[2] != 3 {
		t.Errorf("DecodeValue into a slice = %v, %v; want [1 2 3]", list, err)
	}

	if got := hero.StringValue("fallback"); got != "fallback" {
		t.Errorf("StringValue on a json flag = %q; want the default", got)
	}
	if got := hero.NumberValue(2); got != 2 {
		t.Errorf("NumberValue on a json flag = %v; want the default", got)
	}
}

func TestEvalResult_BooleanFlag_AccessorsReturnTheDefault(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("new_search", true, "boolean", true)
	c := mustNewClient(t, fb.URL)

	result := mustEvaluate(t, c, "new_search")
	if !result.Enabled || result.ValueType != "boolean" || string(result.Value) != "true" {
		t.Fatalf("got Enabled=%v ValueType=%q Value=%s; want true/boolean/true", result.Enabled, result.ValueType, result.Value)
	}
	if got := result.StringValue("fallback"); got != "fallback" {
		t.Errorf("StringValue on a boolean flag = %q; want the default", got)
	}
	if got := result.NumberValue(3); got != 3 {
		t.Errorf("NumberValue on a boolean flag = %v; want the default", got)
	}
	b := defaultBanner
	if err := result.DecodeValue(&b); err == nil || b != defaultBanner {
		t.Errorf("DecodeValue on a boolean flag = %v, v = %+v; want an error and v untouched", err, b)
	}
}

// ---------------------------------------------------------------------------
// A type mismatch or a decode failure gives the default; DecodeValue leaves v as it was
// ---------------------------------------------------------------------------

func TestEvalResult_MismatchOrDecodeFailure_ReturnsTheDefault(t *testing.T) {
	raw := func(s string) json.RawMessage { return json.RawMessage(s) }

	stringCases := []*exp.EvalResult{
		{Key: "k", ValueType: "string", Value: raw(`3`)},
		{Key: "k", ValueType: "string", Value: raw(`null`)},
		{Key: "k", ValueType: "string", Value: raw(`{"a":1}`)},
		{Key: "k", ValueType: "string", Value: raw(`"unterminated`)},
		{Key: "k", ValueType: "number", Value: raw(`"x"`)},
		{Key: "k", ValueType: "", Value: raw(`"x"`)},
	}
	for _, r := range stringCases {
		if got := r.StringValue("fallback"); got != "fallback" {
			t.Errorf("StringValue(%q, %s) = %q; want the default", r.ValueType, r.Value, got)
		}
	}

	numberCases := []*exp.EvalResult{
		{Key: "k", ValueType: "number", Value: raw(`"3"`)},
		{Key: "k", ValueType: "number", Value: raw(`1e400`)},
		{Key: "k", ValueType: "number", Value: raw(`null`)},
		{Key: "k", ValueType: "number", Value: raw(`true`)},
		{Key: "k", ValueType: "string", Value: raw(`3`)},
	}
	for _, r := range numberCases {
		if got := r.NumberValue(-2); got != -2 {
			t.Errorf("NumberValue(%q, %s) = %v; want the default", r.ValueType, r.Value, got)
		}
	}

	decodeCases := []*exp.EvalResult{
		// title decodes before limit fails: v must not be left half-filled.
		{Key: "k", ValueType: "json", Value: raw(`{"title":"Spring sale","limit":"ten"}`)},
		{Key: "k", ValueType: "json", Value: raw(`[1,2]`)},
		{Key: "k", ValueType: "json", Value: raw(`null`)},
		{Key: "k", ValueType: "json", Value: raw(`{"title":`)},
		{Key: "k", ValueType: "string", Value: raw(`{"title":"Spring sale"}`)},
	}
	for _, r := range decodeCases {
		b := defaultBanner
		if err := r.DecodeValue(&b); err == nil {
			t.Errorf("DecodeValue(%q, %s) returned nil; want an error", r.ValueType, r.Value)
		}
		if b != defaultBanner {
			t.Errorf("DecodeValue(%q, %s) changed v to %+v; want it untouched", r.ValueType, r.Value, b)
		}
	}

	good := &exp.EvalResult{Key: "k", ValueType: "json", Value: raw(`{"title":"Spring sale"}`)}
	for _, v := range []any{nil, defaultBanner, (*banner)(nil)} {
		if err := good.DecodeValue(v); err == nil {
			t.Errorf("DecodeValue(%#v) returned nil; want an error for a v that is not a non-nil pointer", v)
		}
	}
}

// ---------------------------------------------------------------------------
// Nil receiver: EvaluateFlag returns nil for an empty key or user
// ---------------------------------------------------------------------------

func TestEvalResult_NilReceiver_ReturnsTheDefault(t *testing.T) {
	c := mustNewClient(t, "http://localhost:9")
	fromClient, err := c.EvaluateFlag(context.Background(), "", newTestUser("u1"))
	if err == nil || fromClient != nil {
		t.Fatalf("EvaluateFlag with an empty key = %v, %v; want nil and an error", fromClient, err)
	}

	for _, r := range []*exp.EvalResult{nil, fromClient} {
		if got := r.StringValue("fallback"); got != "fallback" {
			t.Errorf("StringValue on nil = %q; want the default", got)
		}
		if got := r.NumberValue(1.5); got != 1.5 {
			t.Errorf("NumberValue on nil = %v; want the default", got)
		}
		b := defaultBanner
		if err := r.DecodeValue(&b); err == nil || b != defaultBanner {
			t.Errorf("DecodeValue on nil = %v, v = %+v; want an error and v untouched", err, b)
		}
	}
}

// ---------------------------------------------------------------------------
// Server tolerance (EMP-8): a server without flag values
// ---------------------------------------------------------------------------

// Each new method, against a server that sends no value, returns its
// documented default: EvaluateFlag succeeds, StringValue and NumberValue
// return the caller's default, and DecodeValue returns its error and leaves v
// as the caller set it.
func TestEvalResult_ServerWithoutFlagValues_ReturnsTheDefault(t *testing.T) {
	shapes := map[string]map[string]interface{}{
		// 0.39.0's answer: no value, no value_type.
		"reason-no-value": {"key": "f", "enabled": true, "config": nil, "reason": "rollout"},
		// An older answer: no reason either.
		"no-reason": {"key": "f", "enabled": true, "config": nil},
	}
	for _, valueType := range []string{"string", "number", "json"} {
		// A stored value the server will not serve: value_type and no value key.
		shapes[valueType+"-no-value"] = map[string]interface{}{
			"key": "f", "enabled": true, "value_type": valueType, "config": nil, "reason": "rollout",
		}
		// A null value.
		shapes[valueType+"-null-value"] = map[string]interface{}{
			"key": "f", "enabled": true, "value": nil, "value_type": valueType, "config": nil, "reason": "rollout",
		}
	}
	for name, body := range shapes {
		t.Run(name, func(t *testing.T) {
			fb := newFakeBackend(t)
			fb.respondJSON(http.MethodGet, "/api/v1/feature-flags/evaluate/f", http.StatusOK, body)
			c := mustNewClient(t, fb.URL)

			result, err := c.EvaluateFlag(context.Background(), "f", newTestUser("user-1"))
			if err != nil {
				t.Fatalf("EvaluateFlag error: %v", err)
			}
			if !result.Enabled {
				t.Error("Enabled = false; want the server's true")
			}
			if result.Value != nil {
				t.Errorf("Value = %s; want nil", result.Value)
			}
			if got := result.StringValue("fallback"); got != "fallback" {
				t.Errorf("StringValue = %q; want the default", got)
			}
			if got := result.NumberValue(2.5); got != 2.5 {
				t.Errorf("NumberValue = %v; want the default", got)
			}
			b := defaultBanner
			if err := result.DecodeValue(&b); err == nil || b != defaultBanner {
				t.Errorf("DecodeValue = %v, v = %+v; want its error and v untouched", err, b)
			}
		})
	}

	// A failed evaluation (404) carries no value either.
	fb := newFakeBackend(t)
	c := mustNewClient(t, fb.URL)
	failed, err := c.EvaluateFlag(context.Background(), "missing", newTestUser("user-1"))
	if err == nil {
		t.Fatal("expected an error for a 404")
	}
	if got := failed.StringValue("fallback"); got != "fallback" {
		t.Errorf("StringValue after a failure = %q; want the default", got)
	}
}

// ---------------------------------------------------------------------------
// Value is copied on return: a caller's change cannot reach the cache
// ---------------------------------------------------------------------------

func TestEvaluateFlag_ValueIsCopiedOnReturn(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("hero-banner", true, "json", map[string]interface{}{"title": "Spring sale", "limit": 3})
	c := mustNewClient(t, fb.URL)

	first := mustEvaluate(t, c, "hero-banner") // from the server
	want := append([]byte(nil), first.Value...)
	for i := range first.Value {
		first.Value[i] = 'x'
	}
	first.Enabled = false

	second := mustEvaluate(t, c, "hero-banner") // from the cache
	if fb.Count() != 1 {
		t.Fatalf("requests = %d; want 1 (the second call is a cache hit)", fb.Count())
	}
	if !bytes.Equal(second.Value, want) || !second.Enabled {
		t.Fatalf("after changing the first result, the cache answers Value=%s Enabled=%v; want %s true", second.Value, second.Enabled, want)
	}
	for i := range second.Value {
		second.Value[i] = 'y'
	}

	third := mustEvaluate(t, c, "hero-banner") // from the cache again
	if !bytes.Equal(third.Value, want) {
		t.Fatalf("after changing a cached result, the cache answers Value=%s; want %s", third.Value, want)
	}
	b := defaultBanner
	if err := third.DecodeValue(&b); err != nil || b.Title != "Spring sale" {
		t.Errorf("DecodeValue = %+v, %v", b, err)
	}
}

// Under -race, callers that change the Value they were given must not race
// with one another or with the cache.
func TestEvaluateFlag_ConcurrentCallersOwnTheirValue(t *testing.T) {
	fb := newFakeBackend(t)
	fb.serveValueFlag("checkout-label", true, "string", "Complete purchase")
	c := mustNewClient(t, fb.URL)
	_ = mustEvaluate(t, c, "checkout-label")

	var wg sync.WaitGroup
	errs := make(chan string, 16)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(id byte) {
			defer wg.Done()
			result, err := c.EvaluateFlag(context.Background(), "checkout-label", newTestUser("user-1"))
			if err != nil {
				errs <- err.Error()
				return
			}
			for j := range result.Value {
				result.Value[j] = 'a' + id
			}
			for j := range result.Value {
				if result.Value[j] != 'a'+id {
					errs <- "another caller changed this caller's Value"
					return
				}
			}
		}(byte(i))
	}
	wg.Wait()
	close(errs)
	for msg := range errs {
		t.Error(msg)
	}
	if got := mustEvaluate(t, c, "checkout-label").StringValue("fallback"); got != "Complete purchase" {
		t.Errorf("after concurrent callers changed their values, StringValue = %q", got)
	}
}
