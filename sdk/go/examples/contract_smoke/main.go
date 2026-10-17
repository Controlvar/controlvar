// Command contract_smoke exercises the Go SDK against a live backend using only
// its public API and prints exactly one JSON line on success.
//
//	CONTROLVAR_API_KEY=... go run ./examples/contract_smoke
//
// Environment: CONTROLVAR_API_URL (default http://localhost:8000),
// CONTROLVAR_API_KEY (required), CONTRACT_EXPERIMENT_KEY (default
// sdk_contract_ab), CONTRACT_FLAG_KEY (default sdk_contract_flag),
// CONTRACT_USER_ID (default smoke-<uuid>).
package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"time"

	exp "github.com/controlvar/controlvar/sdk/go"
)

type assignReport struct {
	VariantName string `json:"variant_name"`
	IsControl   bool   `json:"is_control"`
	Sticky      bool   `json:"sticky"`
}

type okReport struct {
	OK bool `json:"ok"`
}

type flagReport struct {
	Enabled bool `json:"enabled"`
}

type report struct {
	SDK    string       `json:"sdk"`
	Assign assignReport `json:"assign"`
	Flag   flagReport   `json:"flag"`
	Track  okReport     `json:"track"`
	Fanout okReport     `json:"fanout"`
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func randomUserID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("smoke-%d", time.Now().UnixNano())
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant
	return fmt.Sprintf("smoke-%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func fail(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "contract_smoke: "+format+"\n", args...)
	os.Exit(1)
}

func main() {
	apiKey := env("EXPERIMENTLY_API_KEY", os.Getenv("CONTROLVAR_API_KEY"))
	if apiKey == "" {
		fail("CONTROLVAR_API_KEY is required")
	}
	baseURL := env("EXPERIMENTLY_API_URL", env("CONTROLVAR_API_URL", "http://localhost:8000"))
	experimentKey := env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
	flagKey := env("CONTRACT_FLAG_KEY", "sdk_contract_flag")
	userID := env("CONTRACT_USER_ID", randomUserID())

	newClient := func() exp.Client {
		c, err := exp.New(exp.WithBaseURL(baseURL), exp.WithAPIKey(apiKey), exp.WithTimeout(10*time.Second))
		if err != nil {
			fail("new client: %v", err)
		}
		return c
	}

	client := newClient()
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	user := &exp.User{ID: userID, Attributes: map[string]interface{}{"source": "contract_smoke"}}

	// 1. Assign twice: the second call is served from the cache; a fresh client
	//    verifies the server itself is sticky.
	first, err := client.GetAssignment(ctx, experimentKey, user)
	if err != nil {
		fail("assign #1: %v", err)
	}
	second, err := client.GetAssignment(ctx, experimentKey, user)
	if err != nil {
		fail("assign #2: %v", err)
	}
	fresh := newClient()
	third, err := fresh.GetAssignment(ctx, experimentKey, user)
	fresh.Close()
	if err != nil {
		fail("assign #3 (fresh client): %v", err)
	}
	if first.VariantName != "control" && first.VariantName != "treatment" {
		fail("unexpected variant_name %q", first.VariantName)
	}
	sticky := first.VariantName == second.VariantName && first.VariantID == second.VariantID &&
		first.VariantName == third.VariantName && first.VariantID == third.VariantID
	if !sticky {
		fail("assignment not sticky: %q / %q / %q", first.VariantName, second.VariantName, third.VariantName)
	}

	// 2. Evaluate the flag.
	flag, err := client.EvaluateFlag(ctx, flagKey, user)
	if err != nil {
		fail("evaluate flag: %v", err)
	}

	// 3. Track a conversion attributed to the experiment.
	if err := client.Track(ctx, &exp.TrackEvent{
		UserID:        userID,
		EventName:     "purchase",
		ExperimentKey: experimentKey,
		Value:         exp.Float64(12.5),
		Properties:    map[string]interface{}{"sdk": "go"},
	}); err != nil {
		fail("track purchase: %v", err)
	}

	// 4. Track without a key: fans out to the cached assignment + flag, then a
	//    2-event batch.
	if err := client.Track(ctx, &exp.TrackEvent{UserID: userID, EventName: "page_view"}); err != nil {
		fail("track page_view (fan-out): %v", err)
	}
	if err := client.TrackBatch(ctx, []*exp.TrackEvent{
		{UserID: userID, EventName: "page_view", ExperimentKey: experimentKey},
		{UserID: userID, EventName: "page_view", FeatureFlagKey: flagKey},
	}); err != nil {
		fail("track batch: %v", err)
	}

	out, err := json.Marshal(report{
		SDK:    "go",
		Assign: assignReport{VariantName: first.VariantName, IsControl: first.IsControl, Sticky: sticky},
		Flag:   flagReport{Enabled: flag.Enabled},
		Track:  okReport{OK: true},
		Fanout: okReport{OK: true},
	})
	if err != nil {
		fail("encode report: %v", err)
	}
	fmt.Println(string(out))
}
