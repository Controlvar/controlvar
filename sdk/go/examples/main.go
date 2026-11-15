// Package main demonstrates the Controlvar Go SDK against a
// running backend (defaults: http://localhost:8000, key from CONTROLVAR_API_KEY;
// earlier versions of this example read EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY).
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	exp "github.com/controlvar/controlvar/sdk/go"
)

func main() {
	baseURL := os.Getenv("CONTROLVAR_API_URL")
	if baseURL == "" {
		baseURL = "http://localhost:8000"
	}

	// Create a client with functional options.
	client, err := exp.New(
		exp.WithBaseURL(baseURL),
		exp.WithAPIKey(os.Getenv("CONTROLVAR_API_KEY")),
		exp.WithCacheSize(500),
		exp.WithCacheTTL(5*time.Minute),
		exp.WithTimeout(10*time.Second),
	)
	if err != nil {
		log.Fatalf("failed to create client: %v", err)
	}
	defer client.Close()

	ctx := context.Background()

	// Attributes are sent as "context" on assignment and flag evaluation, for
	// targeting rules.
	user := &exp.User{
		ID: "user-123",
		Attributes: map[string]interface{}{
			"plan":    "pro",
			"country": "US",
		},
	}

	// -----------------------------------------------------------------------
	// Evaluate a feature flag (server decides; result cached per user + key)
	// -----------------------------------------------------------------------
	flag, err := client.EvaluateFlag(ctx, "new-dashboard", user)
	if err != nil {
		log.Printf("EvaluateFlag error (flag reported off): %v", err)
	}
	fmt.Printf("Flag %q: enabled=%v config=%v\n", flag.Key, flag.Enabled, flag.Config)

	// -----------------------------------------------------------------------
	// Assign the user to an experiment (sticky server-side)
	// -----------------------------------------------------------------------
	assignment, err := client.GetAssignment(ctx, "checkout-flow-experiment", user)
	if err != nil {
		log.Printf("GetAssignment error: %v", err)
	} else {
		fmt.Printf("Experiment %q: variant=%q control=%v configuration=%v\n",
			assignment.ExperimentKey, assignment.VariantName, assignment.IsControl, assignment.Configuration)
	}

	// -----------------------------------------------------------------------
	// Track a conversion for one experiment
	// -----------------------------------------------------------------------
	if err := client.Track(ctx, &exp.TrackEvent{
		UserID:        user.ID,
		EventName:     "purchase",
		ExperimentKey: "checkout-flow-experiment",
		Value:         exp.Float64(99.99),
		Properties:    map[string]interface{}{"currency": "USD", "item_id": "sku-42"},
	}); err != nil {
		log.Printf("Track error: %v", err)
	}

	// -----------------------------------------------------------------------
	// Track without a key: fanned out to every cached experiment + flag
	// -----------------------------------------------------------------------
	if err := client.Track(ctx, &exp.TrackEvent{UserID: user.ID, EventName: "page_view"}); err != nil {
		log.Printf("Track error: %v", err)
	}

	// The MD5 consistent hash is still exported for parity checks.
	fmt.Printf("ConsistentHash(user-123, my-flag) = %.16f\n", exp.ConsistentHash("user-123", "my-flag"))
}
