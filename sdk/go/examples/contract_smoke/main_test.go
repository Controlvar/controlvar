package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"testing"
)

// fakeAPI answers like the API for every request the default transport would
// have sent, and records it: nothing reaches the network.
type fakeAPI struct {
	mu   sync.Mutex
	seen []seenRequest
}

type seenRequest struct {
	Method, URL, Key string
	Body             map[string]interface{}
}

func (f *fakeAPI) RoundTrip(req *http.Request) (*http.Response, error) {
	var body map[string]interface{}
	if req.Body != nil {
		raw, _ := io.ReadAll(req.Body)
		_ = json.Unmarshal(raw, &body)
	}
	f.mu.Lock()
	f.seen = append(f.seen, seenRequest{req.Method, req.URL.String(), req.Header.Get("X-API-Key"), body})
	f.mu.Unlock()
	answer := map[string]interface{}{}
	switch {
	case req.URL.Path == "/api/v1/tracking/assign":
		answer = map[string]interface{}{
			"experiment_key": body["experiment_key"], "user_id": body["user_id"], "variant_id": "v-1",
			"variant_name": "control", "is_control": true, "configuration": nil,
		}
	case strings.HasPrefix(req.URL.Path, "/api/v1/feature-flags/evaluate/"):
		answer = map[string]interface{}{"key": strings.TrimPrefix(req.URL.Path, "/api/v1/feature-flags/evaluate/"), "enabled": true, "config": nil}
	case req.URL.Path == "/api/v1/tracking/batch":
		events, _ := body["events"].([]interface{})
		answer = map[string]interface{}{"success_count": len(events), "failure_count": 0, "errors": []interface{}{}}
	}
	out, _ := json.Marshal(answer)
	return &http.Response{
		StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
		Body: io.NopCloser(bytes.NewReader(out)), Request: req,
	}, nil
}

// run calls main with the given environment and the fake API in place of the
// default transport, and returns what it printed and what it sent.
func run(t *testing.T, env map[string]string) (string, []seenRequest) {
	t.Helper()
	for _, name := range []string{"EXPERIMENTLY_API_URL", "EXPERIMENTLY_API_KEY", "CONTRACT_EXPERIMENT_KEY", "CONTRACT_FLAG_KEY", "CONTRACT_USER_ID"} {
		t.Setenv(name, env[name])
	}
	api := &fakeAPI{}
	saved := http.DefaultTransport
	http.DefaultTransport = api
	t.Cleanup(func() { http.DefaultTransport = saved })

	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout := os.Stdout
	os.Stdout = writer
	main()
	os.Stdout = stdout
	writer.Close()
	printed, _ := io.ReadAll(reader)
	return string(printed), api.seen
}

func TestContractSmoke_ReadsTheEnvironmentWithTheseDefaults(t *testing.T) {
	printed, seen := run(t, map[string]string{"EXPERIMENTLY_API_KEY": "smoke-key"})
	if len(seen) == 0 {
		t.Fatal("no request was made")
	}
	for _, r := range seen {
		if !strings.HasPrefix(r.URL, "http://localhost:8000/api/v1/") || r.Key != "smoke-key" {
			t.Errorf("request %s %s with key %q", r.Method, r.URL, r.Key)
		}
	}
	if seen[0].Body["experiment_key"] != "sdk_contract_ab" {
		t.Errorf("experiment key %v", seen[0].Body["experiment_key"])
	}
	user, _ := seen[0].Body["user_id"].(string)
	if !regexp.MustCompile(`^smoke-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).MatchString(user) {
		t.Errorf("user id %q is not smoke-<uuid>", user)
	}
	evaluated := false
	for _, r := range seen {
		evaluated = evaluated || strings.Contains(r.URL, "/feature-flags/evaluate/sdk_contract_flag?")
	}
	if !evaluated {
		t.Error("sdk_contract_flag was not evaluated")
	}
	if strings.Count(printed, "\n") != 1 {
		t.Errorf("printed %q, not one line", printed)
	}
}

func TestContractSmoke_AssignsThreeTimesEvaluatesTracksAndBatches(t *testing.T) {
	printed, seen := run(t, map[string]string{
		"EXPERIMENTLY_API_URL": "http://api.test", "EXPERIMENTLY_API_KEY": "k",
		"CONTRACT_EXPERIMENT_KEY": "exp_a", "CONTRACT_FLAG_KEY": "flag_a", "CONTRACT_USER_ID": "user-a",
	})
	var calls []string
	for _, r := range seen {
		path := strings.SplitN(strings.TrimPrefix(r.URL, "http://api.test"), "?", 2)[0]
		calls = append(calls, r.Method+" "+path)
	}
	// Twice on one client (the second from its cache), once on a fresh client.
	want := []string{
		"POST /api/v1/tracking/assign",
		"POST /api/v1/tracking/assign",
		"GET /api/v1/feature-flags/evaluate/flag_a",
		"POST /api/v1/tracking/track",
		"POST /api/v1/tracking/batch",
		"POST /api/v1/tracking/batch",
	}
	if strings.Join(calls, "\n") != strings.Join(want, "\n") {
		t.Fatalf("requests:\n%s\nwant:\n%s", strings.Join(calls, "\n"), strings.Join(want, "\n"))
	}
	if seen[3].Body["event_name"] != "purchase" || seen[3].Body["experiment_key"] != "exp_a" {
		t.Errorf("track body %v", seen[3].Body)
	}
	if events, _ := seen[5].Body["events"].([]interface{}); len(events) != 2 {
		t.Errorf("the explicit batch has %d events", len(events))
	}
	var report map[string]interface{}
	if err := json.Unmarshal([]byte(printed), &report); err != nil {
		t.Fatalf("printed %q: %v", printed, err)
	}
	if report["sdk"] != "go" || report["assign"].(map[string]interface{})["sticky"] != true {
		t.Errorf("report %v", report)
	}
}
