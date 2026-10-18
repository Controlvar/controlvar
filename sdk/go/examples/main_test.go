package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
)

type fakeAPI struct{ calls []string }

func (f *fakeAPI) RoundTrip(req *http.Request) (*http.Response, error) {
	f.calls = append(f.calls, req.Method+" "+req.URL.Scheme+"://"+req.URL.Host+req.URL.Path)
	answer := map[string]interface{}{}
	switch {
	case req.URL.Path == "/api/v1/tracking/assign":
		answer = map[string]interface{}{
			"experiment_key": "checkout-flow-experiment", "user_id": "user-123", "variant_id": "v-2",
			"variant_name": "treatment", "is_control": false, "configuration": nil,
		}
	case strings.HasPrefix(req.URL.Path, "/api/v1/feature-flags/evaluate/"):
		answer = map[string]interface{}{"key": "new-dashboard", "enabled": true, "config": nil}
	case req.URL.Path == "/api/v1/tracking/batch":
		answer = map[string]interface{}{"success_count": 2, "failure_count": 0, "errors": []interface{}{}}
	}
	out, _ := json.Marshal(answer)
	return &http.Response{
		StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}},
		Body: io.NopCloser(bytes.NewReader(out)), Request: req,
	}, nil
}

// The walkthrough runs against the backend at CONTROLVAR_API_URL, by default
// http://localhost:8000: a flag, an assignment, a keyed track and a fanned-out one.
func TestWalkthrough_RunsAgainstTheBackend(t *testing.T) {
	t.Setenv("CONTROLVAR_API_URL", "")
	t.Setenv("CONTROLVAR_API_KEY", "walkthrough-key")
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

	want := []string{
		"GET http://localhost:8000/api/v1/feature-flags/evaluate/new-dashboard",
		"POST http://localhost:8000/api/v1/tracking/assign",
		"POST http://localhost:8000/api/v1/tracking/track",
		"POST http://localhost:8000/api/v1/tracking/batch",
	}
	if strings.Join(api.calls, "\n") != strings.Join(want, "\n") {
		t.Fatalf("requests:\n%s\nwant:\n%s", strings.Join(api.calls, "\n"), strings.Join(want, "\n"))
	}
	for _, line := range []string{`Flag "new-dashboard": enabled=true`, `variant="treatment"`, "ConsistentHash(user-123, my-flag) = 0.6927449859213084"} {
		if !strings.Contains(string(printed), line) {
			t.Errorf("the walkthrough did not print %q:\n%s", line, printed)
		}
	}
}
