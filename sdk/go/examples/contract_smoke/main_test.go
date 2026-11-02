package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
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
	// assignUser, when set, is the user_id every /assign answer names.
	assignUser string
	// noAssigned leaves "assigned" out of the /assign answer, as a server
	// from before the field does.
	noAssigned bool
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
			"assigned": true, "reason": "assigned",
		}
		if f.assignUser != "" {
			answer["user_id"] = f.assignUser
		}
		if f.noAssigned {
			delete(answer, "assigned")
			delete(answer, "reason")
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
	return runWith(t, env, &fakeAPI{})
}

// runWith is run with the fake API given.
func runWith(t *testing.T, env map[string]string, api *fakeAPI) (string, []seenRequest) {
	t.Helper()
	for _, name := range []string{"CONTROLVAR_API_URL", "CONTROLVAR_API_KEY", "EXPERIMENTLY_API_URL", "EXPERIMENTLY_API_KEY", "CONTRACT_EXPERIMENT_KEY", "CONTRACT_FLAG_KEY", "CONTRACT_USER_ID"} {
		t.Setenv(name, env[name])
	}
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
	printed, seen := run(t, map[string]string{"CONTROLVAR_API_KEY": "smoke-key"})
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
		"CONTROLVAR_API_URL": "http://api.test", "CONTROLVAR_API_KEY": "k",
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
		// A new client with WithOnExposure, twice (the second from its cache),
		// then a client whose function panics.
		"POST /api/v1/tracking/assign",
		"POST /api/v1/tracking/assign",
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
	exposure, _ := json.Marshal(report["exposure"])
	wantExposure := `{"calls":1,"payload":{"experiment_key":"exp_a","user_id":"user-a","variant_id":"v-1","variant_name":"control"}}`
	if string(exposure) != wantExposure {
		t.Errorf("exposure %s, want %s", exposure, wantExposure)
	}
}

// Against a server that does not send "assigned", the function is not
// called, and the smoke says so (calls 0, no payload) for the runner to
// refuse, instead of failing on its own.
func TestContractSmoke_ReportsNoCallWithoutAssigned(t *testing.T) {
	printed, _ := runWith(t, map[string]string{"CONTROLVAR_API_KEY": "k", "CONTRACT_USER_ID": "user-a"}, &fakeAPI{noAssigned: true})
	var report map[string]interface{}
	if err := json.Unmarshal([]byte(printed), &report); err != nil {
		t.Fatalf("printed %q: %v", printed, err)
	}
	exposure, _ := json.Marshal(report["exposure"])
	if string(exposure) != `{"calls":0,"payload":null}` {
		t.Errorf("exposure %s", exposure)
	}
}

// The runner does not know the smoke's user, so the smoke checks the user ID
// the function received, and exits 1 naming it when it is not the smoke's.
func TestContractSmoke_ExitsWhenTheFunctionGetsAnotherUser(t *testing.T) {
	if os.Getenv("CONTRACT_SMOKE_CHILD") == "1" {
		runWith(t, map[string]string{"CONTROLVAR_API_KEY": "k", "CONTRACT_USER_ID": "user-a"}, &fakeAPI{assignUser: "someone-else"})
		return // main returned: the parent sees exit 0 and fails
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestContractSmoke_ExitsWhenTheFunctionGetsAnotherUser$")
	cmd.Env = append(os.Environ(), "CONTRACT_SMOKE_CHILD=1")
	out, err := cmd.CombinedOutput()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 1 {
		t.Fatalf("the smoke exited with %v, want exit status 1; output:\n%s", err, out)
	}
	if want := `contract_smoke: onExposure received user "someone-else", expected "user-a"`; !strings.Contains(string(out), want) {
		t.Fatalf("output does not say %q:\n%s", want, out)
	}
}

// The new names win; the former ones are still read. Three distinct values per
// setting, none of them the default or what CI sets.
func TestContractSmoke_TheNewNamesWinAndTheFormerNamesAreStillRead(t *testing.T) {
	const newURL, oldURL = "http://127.0.0.1:9", "http://127.0.0.1:7"
	cases := []struct {
		name     string
		env      map[string]string
		url, key string
	}{
		{"former names only", map[string]string{"EXPERIMENTLY_API_URL": oldURL, "EXPERIMENTLY_API_KEY": "old-key"}, oldURL, "old-key"},
		{"new names only", map[string]string{"CONTROLVAR_API_URL": newURL, "CONTROLVAR_API_KEY": "new-key"}, newURL, "new-key"},
		{"both: the new names win", map[string]string{
			"CONTROLVAR_API_URL": newURL, "CONTROLVAR_API_KEY": "new-key",
			"EXPERIMENTLY_API_URL": oldURL, "EXPERIMENTLY_API_KEY": "old-key",
		}, newURL, "new-key"},
		{"no URL: the default", map[string]string{"EXPERIMENTLY_API_KEY": "old-key"}, "http://localhost:8000", "old-key"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, seen := run(t, c.env)
			if len(seen) == 0 {
				t.Fatal("no request was made")
			}
			for _, r := range seen {
				if !strings.HasPrefix(r.URL, c.url+"/api/v1/") || r.Key != c.key {
					t.Errorf("request %s %s with key %q, want %s and %q", r.Method, r.URL, r.Key, c.url, c.key)
				}
			}
		})
	}
}
