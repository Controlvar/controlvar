package experimentation

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The guard's error for a panic in the exposure function is of its own kind,
// errExposureHook, the counterpart of the JavaScript SDK's HOOK_ERROR: its
// text starts "onExposure failed: ", and it wraps the panic value with %w.
// The client has no channel for it (see client.exposed), so this test is
// where its shape is held.
func TestCallExposureHook_APanicIsAHookError(t *testing.T) {
	cause := errors.New("hook boom")
	cases := []struct {
		name  string
		value interface{}
		text  string
	}{
		{"an error", cause, "onExposure failed: hook boom"},
		{"a string", "hook boom", "onExposure failed: hook boom"},
		{"an int", 42, "onExposure failed: 42"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := callExposureHook(context.Background(), func(context.Context, Exposure) { panic(tc.value) }, Exposure{})
			if err == nil {
				t.Fatal("the guard returned nil for a panic")
			}
			if !errors.Is(err, errExposureHook) {
				t.Errorf("errors.Is(err, errExposureHook) is false for %q", err)
			}
			if err.Error() != tc.text || !strings.HasPrefix(err.Error(), "onExposure failed: ") {
				t.Errorf("text %q, want %q", err.Error(), tc.text)
			}
			var urlErr *url.Error
			var apiErr *APIError
			if errors.As(err, &urlErr) || errors.As(err, &apiErr) {
				t.Errorf("%q is reported as a request error", err)
			}
		})
	}
	err := callExposureHook(context.Background(), func(context.Context, Exposure) { panic(cause) }, Exposure{})
	if !errors.Is(err, cause) {
		t.Errorf("%q does not wrap what the function panicked with", err)
	}
}

// A value whose Error or String method panics on every call, so that fmt's own
// recovery of the first panic panics again.
type panicsWhenFormatted struct{}

func (panicsWhenFormatted) Error() string { panic(panicsWhenFormatted{}) }

type panicsWhenStringified struct{}

func (panicsWhenStringified) String() string { panic(panicsWhenStringified{}) }

// A panic value that cannot be formatted is still recovered: the guard returns
// a hook error with a fixed text and nothing panics out of it.
func TestCallExposureHook_AValueThatCannotBeFormattedIsStillContained(t *testing.T) {
	for name, value := range map[string]interface{}{"an error": panicsWhenFormatted{}, "a Stringer": panicsWhenStringified{}} {
		t.Run(name, func(t *testing.T) {
			var err error
			func() {
				defer func() {
					if escaped := recover(); escaped != nil {
						t.Fatalf("a panic of type %T escaped the guard", escaped)
					}
				}()
				err = callExposureHook(context.Background(), func(context.Context, Exposure) { panic(value) }, Exposure{})
			}()
			if !errors.Is(err, errExposureHook) {
				t.Fatalf("got %v, want a hook error", err)
			}
			if want := "onExposure failed: the panic value cannot be turned into a string"; err.Error() != want {
				t.Errorf("text %q, want %q", err.Error(), want)
			}
		})
	}
}

// panic(nil) is recovered too (a *runtime.PanicNilError since Go 1.21).
func TestCallExposureHook_PanicNilIsAHookError(t *testing.T) {
	err := callExposureHook(context.Background(), func(context.Context, Exposure) { panic(nil) }, Exposure{})
	var nilPanic *runtime.PanicNilError
	if !errors.Is(err, errExposureHook) || !errors.As(err, &nilPanic) {
		t.Fatalf("panic(nil) gave %v", err)
	}
}

func TestCallExposureHook_AFunctionThatReturnsIsNoError(t *testing.T) {
	var got Exposure
	want := Exposure{ExperimentKey: "exp-1", UserID: "u-1", VariantID: "v-2", VariantName: "treatment"}
	if err := callExposureHook(context.Background(), func(_ context.Context, e Exposure) { got = e }, want); err != nil {
		t.Fatalf("err = %v", err)
	}
	if got != want {
		t.Fatalf("the function got %+v", got)
	}
}

// A request that fails is not of the hook's kind: the two are told apart.
func TestCallExposureHook_ARequestErrorIsNotAHookError(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closed.Close()
	h := NewHTTPClient(closed.URL, "k", time.Second)
	err := h.Post(context.Background(), "/api/v1/tracking/assign", map[string]string{}, nil)
	if err == nil {
		t.Fatal("a closed server answered")
	}
	if errors.Is(err, errExposureHook) || strings.HasPrefix(err.Error(), "onExposure failed: ") {
		t.Fatalf("a network error reads as a hook error: %q", err)
	}
}

// fill reports an empty or expired slot as filled, and a live one as held.
func TestCache_FillReportsWhetherTheSlotWasEmpty(t *testing.T) {
	c := NewCache(10, time.Hour)
	if !c.fill("k", 1) {
		t.Fatal("an empty slot was reported held")
	}
	if c.fill("k", 2) {
		t.Fatal("a live slot was reported filled")
	}
	if v, _ := c.Get("k"); v != 2 {
		t.Fatalf("fill did not store the new value: %v", v)
	}
	expiring := NewCache(10, time.Nanosecond)
	expiring.fill("k", 1)
	time.Sleep(2 * time.Millisecond) // past the 1 ns lifetime; no timing is asserted
	if !expiring.fill("k", 2) {
		t.Fatal("an expired slot was reported held")
	}
}

// Of goroutines filling the same empty slot at once, exactly one is first: the
// check and the store are one step. A check made apart from the store lets
// two of them see the slot empty; being a race, that is caught in most runs,
// not in every one. A fill that holds the lock across both never fails this.
func TestCache_FillIsOneStep(t *testing.T) {
	const rounds, goroutines = 2000, 8
	for round := 0; round < rounds; round++ {
		c := NewCache(10, time.Hour)
		start := make(chan struct{})
		results := make(chan bool, goroutines)
		for g := 0; g < goroutines; g++ {
			go func(value int) {
				<-start
				results <- c.fill("k", value)
			}(round)
		}
		close(start)
		filled := 0
		for g := 0; g < goroutines; g++ {
			if <-results {
				filled++
			}
		}
		if filled != 1 {
			t.Fatalf("round %d: %d goroutines filled the same slot, want 1", round, filled)
		}
	}
}

// benchHook is a package variable so that neither benchmark can inline the
// call: both make the same indirect call.
var benchHook = func(context.Context, Exposure) {}

// What the guard costs on every exposure: a deferred recover around the
// indirect call. Recorded in the pull request, never asserted.
func BenchmarkCallExposureHook(b *testing.B) {
	e := Exposure{ExperimentKey: "exp-1", UserID: "u-1", VariantID: "v-2", VariantName: "treatment"}
	ctx := context.Background()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		_ = callExposureHook(ctx, benchHook, e)
	}
}

// The same call without the guard, for comparison.
func BenchmarkExposureHookUnguarded(b *testing.B) {
	e := Exposure{ExperimentKey: "exp-1", UserID: "u-1", VariantID: "v-2", VariantName: "treatment"}
	ctx := context.Background()
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		benchHook(ctx, e)
	}
}
