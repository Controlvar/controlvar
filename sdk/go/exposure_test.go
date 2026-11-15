package experimentation_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	exp "github.com/controlvar/controlvar/sdk/go"
)

// ---------------------------------------------------------------------------
// WithOnExposure (#1499): called once per experiment exposure the server
// recorded, that is once per POST /api/v1/tracking/assign answered with
// "assigned": true whose answer the client stores with no unexpired answer
// cached for that user and experiment.
// ---------------------------------------------------------------------------

// exposureLog records every call of its hook.
type exposureLog struct {
	mu    sync.Mutex
	calls []exp.Exposure
}

func (l *exposureLog) hook(_ context.Context, e exp.Exposure) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.calls = append(l.calls, e)
}

func (l *exposureLog) all() []exp.Exposure {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]exp.Exposure(nil), l.calls...)
}

// exposureServer answers the assign and flag routes and records each request
// as "METHOD path". answer gives the assign route's status and body for the
// request's decoded body.
type exposureServer struct {
	*httptest.Server
	mu   sync.Mutex
	seen []string
}

func newExposureServer(t *testing.T, answer func(body map[string]interface{}) (int, interface{})) *exposureServer {
	t.Helper()
	s := &exposureServer{}
	s.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		s.mu.Lock()
		s.seen = append(s.seen, r.Method+" "+r.URL.Path)
		s.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/tracking/assign":
			status, out := answer(body)
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(out)
		case strings.HasPrefix(r.URL.Path, "/api/v1/feature-flags/evaluate/"):
			key := strings.TrimPrefix(r.URL.Path, "/api/v1/feature-flags/evaluate/")
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"key": key, "enabled": true, "config": nil})
		default:
			http.Error(w, `{"detail":"no route"}`, http.StatusNotFound)
		}
	}))
	t.Cleanup(s.Close)
	return s
}

func (s *exposureServer) requests() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.seen...)
}

// enrolled answers as the server does for a user the experiment enrolled
// (VariantAssignmentResponse's fields): the request's experiment key and user
// ID, "treatment" (the default would be "control", so a fallback cannot pass
// for it), and "assigned": true.
func enrolled(body map[string]interface{}) (int, interface{}) {
	return http.StatusOK, map[string]interface{}{
		"experiment_key": body["experiment_key"], "user_id": body["user_id"],
		"variant_id": "v-2", "variant_name": "treatment", "is_control": false,
		"configuration": nil, "assigned": true, "reason": "assigned",
	}
}

func assignOK(t *testing.T, c exp.Client, experimentKey, userID string) *exp.Assignment {
	t.Helper()
	a, err := c.GetAssignment(context.Background(), experimentKey, newTestUser(userID))
	if err != nil {
		t.Fatalf("GetAssignment(%q, %q): %v", experimentKey, userID, err)
	}
	return a
}

func wantExposures(t *testing.T, got []exp.Exposure, want ...exp.Exposure) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("hook calls = %d, want %d: %+v", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("hook call %d = %+v, want %+v", i+1, got[i], want[i])
		}
	}
}

func wantRequests(t *testing.T, s *exposureServer, want int) {
	t.Helper()
	if got := len(s.requests()); got != want {
		t.Fatalf("requests = %d, want %d: %v", got, want, s.requests())
	}
}

func exposureOf(experimentKey, userID string) exp.Exposure {
	return exp.Exposure{ExperimentKey: experimentKey, UserID: userID, VariantID: "v-2", VariantName: "treatment"}
}

func TestOnExposure_CalledOnceWithTheFourValues(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	a := assignOK(t, c, "exp-1", "u-1")
	if a.VariantName != "treatment" {
		t.Fatalf("answer %q, want treatment", a.VariantName)
	}
	wantExposures(t, log.all(), exp.Exposure{ExperimentKey: "exp-1", UserID: "u-1", VariantID: "v-2", VariantName: "treatment"})
}

func TestOnExposure_VariantIDIsEmptyWhenTheServerSendsNone(t *testing.T) {
	s := newExposureServer(t, func(body map[string]interface{}) (int, interface{}) {
		_, out := enrolled(body)
		delete(out.(map[string]interface{}), "variant_id")
		return http.StatusOK, out
	})
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	assignOK(t, c, "exp-1", "u-1")
	wantExposures(t, log.all(), exp.Exposure{ExperimentKey: "exp-1", UserID: "u-1", VariantID: "", VariantName: "treatment"})
}

func TestOnExposure_ACachedAnswerCallsNothing(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	for i := 0; i < 5; i++ {
		assignOK(t, c, "exp-1", "u-1")
	}
	wantRequests(t, s, 1)
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"))
}

func TestOnExposure_OncePerUserAndExperiment(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	for round := 0; round < 2; round++ {
		assignOK(t, c, "exp-1", "u-1")
		assignOK(t, c, "exp-1", "u-2")
		assignOK(t, c, "exp-2", "u-1")
	}
	wantRequests(t, s, 3)
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"), exposureOf("exp-1", "u-2"), exposureOf("exp-2", "u-1"))
}

// Two first calls for the same user and experiment, both in flight before
// either is answered: each sends a request (the server records two exposures),
// and only the first answer stored calls the hook. The barrier is the server's:
// it answers when the second request arrives, or 500 after 5 s, so a call that
// never got there fails the test instead of passing it.
func TestOnExposure_ConcurrentFirstCalls_TwoRequestsOneCall(t *testing.T) {
	var mu sync.Mutex
	arrived := 0
	release := make(chan struct{})
	s := newExposureServer(t, func(body map[string]interface{}) (int, interface{}) {
		mu.Lock()
		arrived++
		if arrived == 2 {
			close(release)
		}
		mu.Unlock()
		select {
		case <-release:
			return enrolled(body)
		case <-time.After(5 * time.Second):
			return http.StatusInternalServerError, map[string]string{"detail": "the second request never came"}
		}
	})
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	var wg sync.WaitGroup
	answers := make([]*exp.Assignment, 2)
	errs := make([]error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			answers[i], errs[i] = c.GetAssignment(context.Background(), "exp-1", newTestUser("u-1"))
		}(i)
	}
	wg.Wait()
	for i := range errs {
		if errs[i] != nil || answers[i] == nil || answers[i].VariantName != "treatment" {
			t.Fatalf("call %d: %+v, %v", i+1, answers[i], errs[i])
		}
	}
	wantRequests(t, s, 2)
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"))
}

// The server records nothing for a user it did not enrol or an experiment
// that is not running; it answers the control variant with "assigned": false.
func TestOnExposure_NotCalledForAssignedFalse(t *testing.T) {
	for _, reason := range []string{"holdout", "mutual_exclusion", "targeting", "draft", "paused", "completed", "archived"} {
		t.Run(reason, func(t *testing.T) {
			s := newExposureServer(t, func(body map[string]interface{}) (int, interface{}) {
				return http.StatusOK, map[string]interface{}{
					"experiment_key": body["experiment_key"], "user_id": body["user_id"],
					"variant_id": "v-1", "variant_name": "control", "is_control": true,
					"configuration": nil, "assigned": false, "reason": reason,
				}
			})
			log := &exposureLog{}
			c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

			a := assignOK(t, c, "exp-1", "u-1")
			if a.VariantName != "control" || !a.IsControl || a.VariantID != "v-1" {
				t.Errorf("answer changed: %+v", a)
			}
			wantRequests(t, s, 1)
			wantExposures(t, log.all())
		})
	}
}

// An answer whose "assigned" is missing (a server from before the field), or
// is anything but true, is answered as before and calls nothing.
func TestOnExposure_ServerWithoutAssigned_AnswersAndCallsNothing(t *testing.T) {
	cases := map[string]interface{}{"missing": nil, "null": "null", "the string true": "true", "the number 1": 1}
	for name, value := range cases {
		t.Run(name, func(t *testing.T) {
			s := newExposureServer(t, func(body map[string]interface{}) (int, interface{}) {
				_, out := enrolled(body)
				m := out.(map[string]interface{})
				delete(m, "reason")
				switch value {
				case nil:
					delete(m, "assigned")
				case "null":
					m["assigned"] = nil
				default:
					m["assigned"] = value
				}
				return http.StatusOK, m
			})
			log := &exposureLog{}
			c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

			a := assignOK(t, c, "exp-1", "u-1")
			if a.VariantName != "treatment" || a.VariantID != "v-2" || a.UserID != "u-1" {
				t.Errorf("answer = %+v", a)
			}
			wantExposures(t, log.all())
		})
	}
}

func TestOnExposure_AFailedCallCallsNothing(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closedURL := closed.URL
	closed.Close()

	cases := []struct {
		name string
		url  func(t *testing.T) string
	}{
		{"404", func(t *testing.T) string {
			return newExposureServer(t, func(map[string]interface{}) (int, interface{}) {
				return http.StatusNotFound, map[string]string{"detail": "not found"}
			}).URL
		}},
		{"500", func(t *testing.T) string {
			return newExposureServer(t, func(map[string]interface{}) (int, interface{}) {
				return http.StatusInternalServerError, map[string]string{"detail": "boom"}
			}).URL
		}},
		{"network error", func(*testing.T) string { return closedURL }},
		{"invalid JSON", func(t *testing.T) string {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"experiment_key":"exp-1","assigned":true,`))
			}))
			t.Cleanup(s.Close)
			return s.URL
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			log := &exposureLog{}
			c := mustNewClient(t, tc.url(t), exp.WithOnExposure(log.hook))
			a, err := c.GetAssignment(context.Background(), "exp-1", newTestUser("u-1"))
			if err == nil || a != nil {
				t.Fatalf("GetAssignment = %+v, %v; want nil and an error", a, err)
			}
			wantExposures(t, log.all())
		})
	}
}

// A call that times out calls nothing, even when the server goes on to record
// the exposure: this server answers "assigned": true only once the client has
// given up on the request.
func TestOnExposure_ATimedOutCallCallsNothing(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		select {
		case <-r.Context().Done():
		case <-time.After(5 * time.Second):
		}
		_, out := enrolled(body)
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(s.Close)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook), exp.WithTimeout(20*time.Millisecond))

	a, err := c.GetAssignment(context.Background(), "exp-1", newTestUser("u-1"))
	if err == nil || a != nil {
		t.Fatalf("GetAssignment = %+v, %v; want nil and a timeout error", a, err)
	}
	wantExposures(t, log.all())
}

// unformattableError's Error method panics on every call, so fmt's recovery of
// the first panic panics again.
type unformattableError struct{}

func (unformattableError) Error() string { panic(unformattableError{}) }

// A panic in the function never reaches the caller or changes the answer, and
// the answer stays cached.
func TestOnExposure_APanicChangesNothing(t *testing.T) {
	for name, value := range map[string]interface{}{
		"an error": errors.New("hook boom"), "a string": "hook boom", "nil": nil,
		"an error that cannot be formatted": unformattableError{},
	} {
		t.Run(name, func(t *testing.T) {
			s := newExposureServer(t, enrolled)
			calls := 0
			c := mustNewClient(t, s.URL, exp.WithOnExposure(func(context.Context, exp.Exposure) {
				calls++
				panic(value)
			}))

			a, err := c.GetAssignment(context.Background(), "exp-1", newTestUser("u-1"))
			if err != nil || a == nil || a.VariantName != "treatment" || a.IsControl {
				t.Fatalf("GetAssignment = %+v, %v; want treatment and a nil error", a, err)
			}
			if again := assignOK(t, c, "exp-1", "u-1"); again.VariantName != "treatment" {
				t.Fatalf("second answer %q", again.VariantName)
			}
			wantRequests(t, s, 1)
			if calls != 1 {
				t.Fatalf("hook calls = %d, want 1", calls)
			}
		})
	}
}

// goroutineID is the number in the first line of this goroutine's stack,
// "goroutine 21 [running]:".
func goroutineID() string {
	buf := make([]byte, 64)
	buf = buf[:runtime.Stack(buf, false)]
	if fields := strings.Fields(string(buf)); len(fields) > 1 {
		return fields[1]
	}
	return ""
}

func TestOnExposure_RunsOnTheCallersGoroutineBeforeGetAssignmentReturns(t *testing.T) {
	s := newExposureServer(t, enrolled)
	var events []string
	var hookGoroutine string
	c := mustNewClient(t, s.URL, exp.WithOnExposure(func(context.Context, exp.Exposure) {
		hookGoroutine = goroutineID()
		events = append(events, "hook")
	}))

	caller := goroutineID()
	assignOK(t, c, "exp-1", "u-1")
	events = append(events, "returned")
	if hookGoroutine != caller {
		t.Errorf("hook ran on goroutine %s, caller %s", hookGoroutine, caller)
	}
	if strings.Join(events, ",") != "hook,returned" {
		t.Errorf("events = %v, want [hook returned]", events)
	}
}

// newClientClosedIfPassed is mustNewClient for a test that detects a deadlock:
// it closes the client only when the test passed, because Close waits for the
// cache's lock, and after a detected deadlock that wait would hang the package
// until its -timeout and hide the failure.
func newClientClosedIfPassed(t *testing.T, serverURL string, opts ...exp.Option) exp.Client {
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
	t.Cleanup(func() {
		if !t.Failed() {
			_ = c.Close()
		}
	})
	return c
}

// The function may call the client: asked for the same assignment, the client
// answers from the cache (the answer is stored before the hook), sends nothing
// and holds no lock the call needs.
func TestOnExposure_MayCallGetAssignmentForTheSameKey(t *testing.T) {
	s := newExposureServer(t, enrolled)
	var c exp.Client
	var inner *exp.Assignment
	var innerErr error
	reentered := 0
	c = newClientClosedIfPassed(t, s.URL, exp.WithOnExposure(func(ctx context.Context, e exp.Exposure) {
		if reentered++; reentered > 1 {
			return
		}
		inner, innerErr = c.GetAssignment(ctx, e.ExperimentKey, newTestUser(e.UserID))
	}))

	done := make(chan *exp.Assignment, 1)
	go func() {
		a, _ := c.GetAssignment(context.Background(), "exp-1", newTestUser("u-1"))
		done <- a
	}()
	select {
	case outer := <-done:
		if outer == nil || outer.VariantName != "treatment" {
			t.Fatalf("outer answer %+v", outer)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("GetAssignment did not return: the hook ran under a lock of the client")
	}
	if innerErr != nil || inner == nil || inner.VariantName != "treatment" {
		t.Fatalf("inner answer %+v, %v", inner, innerErr)
	}
	wantRequests(t, s, 1)
	if reentered != 1 {
		t.Fatalf("hook calls = %d, want 1", reentered)
	}
}

// The function holds up only the call it runs in: while it blocks, that
// GetAssignment does not return, and calls for other users and experiments,
// fresh or cached, do.
func TestOnExposure_ABlockedFunctionHoldsUpOnlyItsOwnCall(t *testing.T) {
	s := newExposureServer(t, enrolled)
	entered := make(chan struct{})
	release := make(chan struct{})
	c := newClientClosedIfPassed(t, s.URL, exp.WithOnExposure(func(_ context.Context, e exp.Exposure) {
		if e == exposureOf("exp-1", "u-1") {
			close(entered)
			<-release
		}
	}))
	assignOK(t, c, "exp-cached", "u-2")

	outer := make(chan *exp.Assignment, 1)
	go func() {
		a, _ := c.GetAssignment(context.Background(), "exp-1", newTestUser("u-1"))
		outer <- a
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the function was not called")
	}
	for _, call := range [][2]string{{"exp-2", "u-1"}, {"exp-1", "u-3"}, {"exp-cached", "u-2"}} {
		call := call
		done := make(chan error, 1)
		go func() {
			_, err := c.GetAssignment(context.Background(), call[0], newTestUser(call[1]))
			done <- err
		}()
		select {
		case err := <-done:
			if err != nil {
				t.Fatalf("GetAssignment(%s, %s): %v", call[0], call[1], err)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("GetAssignment(%s, %s) waited for another call's function", call[0], call[1])
		}
	}
	select {
	case a := <-outer:
		t.Fatalf("GetAssignment returned %+v while its function was still running", a)
	default:
	}
	close(release)
	select {
	case a := <-outer:
		if a == nil || a.VariantName != "treatment" {
			t.Fatalf("answer %+v", a)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("GetAssignment did not return once its function did")
	}
}

type ctxKey struct{}

// ctx is the context GetAssignment was given, and cancelling it does not stop
// the function or change the answer.
func TestOnExposure_ReceivesTheContextGetAssignmentWasGiven(t *testing.T) {
	s := newExposureServer(t, enrolled)
	ctx, cancel := context.WithCancel(context.WithValue(context.Background(), ctxKey{}, "trace-1"))
	defer cancel()
	var seen interface{}
	finished := false
	c := mustNewClient(t, s.URL, exp.WithOnExposure(func(hookCtx context.Context, _ exp.Exposure) {
		seen = hookCtx.Value(ctxKey{})
		cancel()
		<-hookCtx.Done()
		finished = true
	}))

	a, err := c.GetAssignment(ctx, "exp-1", newTestUser("u-1"))
	if err != nil || a == nil || a.VariantName != "treatment" {
		t.Fatalf("GetAssignment = %+v, %v", a, err)
	}
	if seen != "trace-1" || !finished {
		t.Errorf("hook saw %v, finished %v", seen, finished)
	}
}

func TestOnExposure_FlagsNeverCallIt(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	for i := 0; i < 2; i++ {
		if _, err := c.EvaluateFlag(context.Background(), "flag-1", newTestUser("u-1")); err != nil {
			t.Fatalf("EvaluateFlag: %v", err)
		}
	}
	wantRequests(t, s, 1)
	wantExposures(t, log.all())
}

func TestOnExposure_CalledAgainAfterTheCacheLifetime(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook), exp.WithCacheTTL(time.Nanosecond))

	assignOK(t, c, "exp-1", "u-1")
	time.Sleep(2 * time.Millisecond) // past the 1 ns lifetime; no timing is asserted
	assignOK(t, c, "exp-1", "u-1")
	wantRequests(t, s, 2)
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"), exposureOf("exp-1", "u-1"))
}

// The cache is one LRU shared by flags and assignments, so another
// experiment's answer, or a flag evaluation, can evict an assignment.
func TestOnExposure_CalledAgainAfterEviction(t *testing.T) {
	t.Run("by another experiment", func(t *testing.T) {
		s := newExposureServer(t, enrolled)
		log := &exposureLog{}
		c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook), exp.WithCacheSize(1))

		assignOK(t, c, "exp-1", "u-1")
		assignOK(t, c, "exp-2", "u-1")
		assignOK(t, c, "exp-1", "u-1")
		wantRequests(t, s, 3)
		wantExposures(t, log.all(), exposureOf("exp-1", "u-1"), exposureOf("exp-2", "u-1"), exposureOf("exp-1", "u-1"))
	})
	t.Run("by a flag evaluation", func(t *testing.T) {
		s := newExposureServer(t, enrolled)
		log := &exposureLog{}
		c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook), exp.WithCacheSize(1))

		assignOK(t, c, "exp-1", "u-1")
		if _, err := c.EvaluateFlag(context.Background(), "flag-1", newTestUser("u-1")); err != nil {
			t.Fatalf("EvaluateFlag: %v", err)
		}
		assignOK(t, c, "exp-1", "u-1")
		wantRequests(t, s, 3)
		wantExposures(t, log.all(), exposureOf("exp-1", "u-1"), exposureOf("exp-1", "u-1"))
	})
}

func TestOnExposure_CalledAgainInANewClient(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	first := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))
	second := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	assignOK(t, first, "exp-1", "u-1")
	assignOK(t, second, "exp-1", "u-1")
	wantRequests(t, s, 2)
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"), exposureOf("exp-1", "u-1"))
}

// With WithCacheTTL(0) or less nothing expires, so the function is called once
// per user and experiment until the entry is evicted.
func TestOnExposure_WithCacheTTLZero_OnceUntilEvicted(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook), exp.WithCacheTTL(0), exp.WithCacheSize(1))

	for i := 0; i < 3; i++ {
		assignOK(t, c, "exp-1", "u-1")
	}
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"))
	assignOK(t, c, "exp-2", "u-1")
	assignOK(t, c, "exp-1", "u-1")
	wantRequests(t, s, 3)
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"), exposureOf("exp-2", "u-1"), exposureOf("exp-1", "u-1"))
}

// The client sends nothing because of the hook: one assign is one request.
func TestOnExposure_SendsNothingOfItsOwn(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	c := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook))

	assignOK(t, c, "exp-1", "u-1")
	if got := strings.Join(s.requests(), "\n"); got != "POST /api/v1/tracking/assign" {
		t.Fatalf("requests:\n%s\nwant exactly POST /api/v1/tracking/assign", got)
	}
	wantExposures(t, log.all(), exposureOf("exp-1", "u-1"))
}

// Each call gets an Exposure of its own: changing it changes nothing cached.
func TestOnExposure_ChangingTheExposureChangesNothingCached(t *testing.T) {
	s := newExposureServer(t, enrolled)
	c := mustNewClient(t, s.URL, exp.WithOnExposure(func(_ context.Context, e exp.Exposure) {
		e.VariantName = "mutated"
		e.UserID = "someone-else"
	}))

	assignOK(t, c, "exp-1", "u-1")
	if again := assignOK(t, c, "exp-1", "u-1"); again.VariantName != "treatment" || again.UserID != "u-1" {
		t.Fatalf("the cached answer is %+v", again)
	}
}

// Exposure is exactly four strings, in the Assignment's order: nothing else
// (no attributes, no configuration) reaches the function.
func TestExposure_HasExactlyFourStringFields(t *testing.T) {
	typ := reflect.TypeOf(exp.Exposure{})
	var fields []string
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		if f.Type.Kind() != reflect.String {
			t.Errorf("%s is a %s, not a string", f.Name, f.Type)
		}
		fields = append(fields, f.Name)
	}
	if got := strings.Join(fields, ","); got != "ExperimentKey,UserID,VariantID,VariantName" {
		t.Fatalf("Exposure fields = %s", got)
	}
}

func TestWithOnExposure_NilSetsNone(t *testing.T) {
	s := newExposureServer(t, enrolled)
	log := &exposureLog{}
	none := mustNewClient(t, s.URL, exp.WithOnExposure(nil))
	cleared := mustNewClient(t, s.URL, exp.WithOnExposure(log.hook), exp.WithOnExposure(nil))

	if a := assignOK(t, none, "exp-1", "u-1"); a.VariantName != "treatment" {
		t.Fatalf("answer %+v", a)
	}
	if a := assignOK(t, cleared, "exp-1", "u-1"); a.VariantName != "treatment" {
		t.Fatalf("answer %+v", a)
	}
	wantRequests(t, s, 2)
	wantExposures(t, log.all())
}

// SdkConfig stays comparable: WithOnExposure keeps its function behind a
// pointer. A func-typed field on SdkConfig would stop this file compiling.
func TestSdkConfig_StaysComparable(t *testing.T) {
	a, b := exp.SdkConfig{BaseURL: "http://x", CacheSize: 1}, exp.SdkConfig{BaseURL: "http://x", CacheSize: 1}
	if a != b {
		t.Fatal("two equal configs compare unequal")
	}
}
