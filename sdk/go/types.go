// Package experimentation provides a Go SDK for Experimently.
//
// Feature flag evaluation and experiment assignment are decided by the
// server: every call goes to the public API with your X-API-Key, the server
// buckets the user (sticky per user + experiment), and the SDK caches the
// answer per user + key for a TTL. Nothing is bucketed locally. The MD5
// consistent hash is still exported as ConsistentHash for parity checks.
package experimentation

import "time"

// User represents the user a flag or experiment is evaluated for.
type User struct {
	// ID is the stable identifier the server uses for bucketing.
	ID string `json:"id"`
	// Attributes are sent as the assignment "context" (used by targeting rules).
	Attributes map[string]interface{} `json:"attributes,omitempty"`
}

// EvalResult is the server's evaluation of one feature flag for one user
// (GET /api/v1/feature-flags/evaluate/{key}?user_id=...).
type EvalResult struct {
	// Key is the evaluated flag key.
	Key string `json:"key"`
	// Enabled is the server's decision for this user.
	Enabled bool `json:"enabled"`
	// Config is nil today: the evaluate route returns null for every flag. It
	// holds the JSON object if the server ever sends one, and is nil for null
	// or a non-object value.
	Config map[string]any `json:"config,omitempty"`
}

// Assignment is the variant the server assigned a user to
// (POST /api/v1/tracking/assign). Assignments are sticky server-side.
type Assignment struct {
	ExperimentKey string `json:"experiment_key"`
	UserID        string `json:"user_id"`
	// VariantID is the variant UUID.
	VariantID string `json:"variant_id"`
	// VariantName is the variant's name, e.g. "control" or "treatment".
	VariantName string `json:"variant_name"`
	IsControl   bool   `json:"is_control"`
	// Configuration is the variant's configuration JSON from the experiment
	// definition, or nil.
	Configuration map[string]any `json:"configuration,omitempty"`
}

// TrackEvent is an analytics event recorded with Client.Track / Client.TrackBatch.
type TrackEvent struct {
	// UserID is required.
	UserID string
	// EventName is the event's name, e.g. "purchase". Experiment metrics are
	// matched by event name. Required unless EventType is set.
	EventName string
	// EventType is sent as event_type; it defaults to EventName.
	EventType string
	// ExperimentKey attributes the event to one experiment. When neither
	// ExperimentKey nor FeatureFlagKey is set the event is fanned out to every
	// experiment/flag cached for the user (see Client.Track).
	ExperimentKey string
	// FeatureFlagKey attributes the event to one feature flag.
	FeatureFlagKey string
	// Value is an optional numeric value (revenue, duration, ...). Use Float64
	// to build the pointer inline.
	Value *float64
	// Properties are sent as the event's metadata.
	Properties map[string]interface{}
	// Timestamp is sent as ISO-8601 when non-zero; the server stamps the event
	// otherwise.
	Timestamp time.Time
}

// Float64 returns a pointer to v, for TrackEvent.Value.
func Float64(v float64) *float64 {
	return &v
}

// trackBody is the wire form of one /api/v1/tracking/track request and of
// each /api/v1/tracking/batch entry.
type trackBody struct {
	EventType      string                 `json:"event_type"`
	EventName      string                 `json:"event_name,omitempty"`
	UserID         string                 `json:"user_id"`
	ExperimentKey  string                 `json:"experiment_key,omitempty"`
	FeatureFlagKey string                 `json:"feature_flag_key,omitempty"`
	Value          *float64               `json:"value,omitempty"`
	Metadata       map[string]interface{} `json:"metadata,omitempty"`
	Timestamp      string                 `json:"timestamp,omitempty"`
}

func (e *TrackEvent) body() trackBody {
	eventType := e.EventType
	if eventType == "" {
		eventType = e.EventName
	}
	b := trackBody{
		EventType:      eventType,
		EventName:      e.EventName,
		UserID:         e.UserID,
		ExperimentKey:  e.ExperimentKey,
		FeatureFlagKey: e.FeatureFlagKey,
		Value:          e.Value,
		Metadata:       e.Properties,
	}
	if !e.Timestamp.IsZero() {
		b.Timestamp = e.Timestamp.UTC().Format(time.RFC3339Nano)
	}
	return b
}

// hasKey reports whether the event is attributed to an experiment or flag.
func (e *TrackEvent) hasKey() bool {
	return e.ExperimentKey != "" || e.FeatureFlagKey != ""
}
