package experimentation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
)

// batchLimit is the maximum number of events per POST /api/v1/tracking/batch.
const batchLimit = 100

// Client is the main interface for interacting with Controlvar.
// Implementations must be safe for concurrent use by multiple goroutines.
type Client interface {
	// EvaluateFlag evaluates a feature flag for the given user via
	// GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=... The server
	// decides; successful results are cached per user + key for the cache TTL.
	// On failure it returns a disabled EvalResult together with the error.
	// Each call returns an EvalResult of its own; a string, number or json
	// flag's value is read with its StringValue, NumberValue or DecodeValue.
	EvaluateFlag(ctx context.Context, flagKey string, user *User) (*EvalResult, error)

	// GetAssignment assigns the user to an experiment variant via
	// POST /api/v1/tracking/assign (sticky server-side; records the exposure).
	// Successful assignments are cached per user + key for the cache TTL.
	// On failure it returns (nil, err).
	GetAssignment(ctx context.Context, experimentKey string, user *User) (*Assignment, error)

	// Track records an analytics event. With ExperimentKey or FeatureFlagKey set
	// it sends one POST /api/v1/tracking/track. Without a key it fans the event
	// out via POST /api/v1/tracking/batch to every experiment the user was
	// assigned to and every flag evaluated for the user by this client (from the
	// cache); if nothing is cached, nothing is sent. Track never panics; the
	// returned error is informational and safe to ignore.
	Track(ctx context.Context, event *TrackEvent) error

	// TrackBatch records several events with at most 100 per request. Events
	// without a key are fanned out like Track.
	TrackBatch(ctx context.Context, events []*TrackEvent) error

	// Close releases all resources held by the client (HTTP connections, caches).
	// It is safe to call more than once. After Close, the client must not be used.
	Close() error
}

// client is the concrete implementation of Client.
type client struct {
	config    *SdkConfig
	http      *HTTPClient
	cache     *Cache // evaluations and assignments, keyed per user + key
	closeOnce sync.Once
}

// cachedFlag / cachedAssignment are the cache values; they carry the user ID
// so Track can enumerate a user's entries without parsing cache keys.
type cachedFlag struct {
	userID string
	result *EvalResult
}

type cachedAssignment struct {
	userID     string
	assignment *Assignment
}

const (
	cacheKindFlag   = "flag"
	cacheKindAssign = "assign"
)

// cacheKey namespaces cache entries by kind, user and key. NUL separators
// keep "a"+"b:c" and "a:b"+"c" distinct.
func cacheKey(kind, userID, key string) string {
	return kind + "\x00" + userID + "\x00" + key
}

// New creates and returns a new SDK Client using the provided functional options.
// The client is ready for use immediately after creation.
//
// Example:
//
//	c, err := experimentation.New(
//	    experimentation.WithBaseURL("https://api.example.com"),
//	    experimentation.WithAPIKey("my-api-key"),
//	    experimentation.WithCacheTTL(5*time.Minute),
//	)
func New(opts ...Option) (Client, error) {
	cfg := defaultConfig()
	for _, opt := range opts {
		opt(cfg)
	}

	cfg.BaseURL = strings.TrimRight(cfg.BaseURL, "/")
	if cfg.BaseURL == "" {
		return nil, errors.New("experimentation: BaseURL must not be empty")
	}

	return &client{
		config: cfg,
		http:   NewHTTPClient(cfg.BaseURL, cfg.APIKey, cfg.Timeout),
		cache:  NewCache(cfg.CacheSize, cfg.CacheTTL),
	}, nil
}

// evaluateResponse is the wire form of GET /api/v1/feature-flags/evaluate/{key}.
// Config and Value are kept raw because the server may return any JSON value.
// A server without flag values sends neither value nor value_type.
type evaluateResponse struct {
	Key       string          `json:"key"`
	Enabled   bool            `json:"enabled"`
	Config    json.RawMessage `json:"config"`
	Value     json.RawMessage `json:"value"`
	ValueType string          `json:"value_type"`
}

// EvaluateFlag implements Client.
func (c *client) EvaluateFlag(ctx context.Context, flagKey string, user *User) (*EvalResult, error) {
	if flagKey == "" {
		return nil, errors.New("experimentation: flagKey must not be empty")
	}
	if user == nil || user.ID == "" {
		return nil, errors.New("experimentation: user and user.ID must not be empty")
	}

	key := cacheKey(cacheKindFlag, user.ID, flagKey)
	if cached, ok := c.cache.Get(key); ok {
		if cf, ok := cached.(*cachedFlag); ok {
			// Every caller gets its own copy: the cached result is shared.
			return cf.result.clone(), nil
		}
	}

	path := "/api/v1/feature-flags/evaluate/" + url.PathEscape(flagKey) +
		"?user_id=" + url.QueryEscape(user.ID)

	var resp evaluateResponse
	if err := c.http.Get(ctx, path, &resp); err != nil {
		// Degrade gracefully: the flag is reported off. Failures are not cached.
		return &EvalResult{Key: flagKey, Enabled: false}, fmt.Errorf("evaluate flag %q: %w", flagKey, err)
	}

	result := &EvalResult{Key: resp.Key, Enabled: resp.Enabled, ValueType: resp.ValueType}
	if result.Key == "" {
		result.Key = flagKey
	}
	if len(resp.Config) > 0 {
		var cfg map[string]any
		if err := json.Unmarshal(resp.Config, &cfg); err == nil {
			result.Config = cfg
		}
	}
	if hasValue(resp.Value) {
		result.Value = resp.Value
	}

	c.cache.Set(key, &cachedFlag{userID: user.ID, result: result})
	return result.clone(), nil
}

// assignRequest is the body of POST /api/v1/tracking/assign.
type assignRequest struct {
	ExperimentKey string                 `json:"experiment_key"`
	UserID        string                 `json:"user_id"`
	Context       map[string]interface{} `json:"context,omitempty"`
}

// GetAssignment implements Client.
func (c *client) GetAssignment(ctx context.Context, experimentKey string, user *User) (*Assignment, error) {
	if experimentKey == "" {
		return nil, errors.New("experimentation: experimentKey must not be empty")
	}
	if user == nil || user.ID == "" {
		return nil, errors.New("experimentation: user and user.ID must not be empty")
	}

	key := cacheKey(cacheKindAssign, user.ID, experimentKey)
	if cached, ok := c.cache.Get(key); ok {
		if ca, ok := cached.(*cachedAssignment); ok {
			return ca.assignment, nil
		}
	}

	payload := assignRequest{
		ExperimentKey: experimentKey,
		UserID:        user.ID,
		Context:       user.Attributes,
	}

	var assignment Assignment
	if err := c.http.Post(ctx, "/api/v1/tracking/assign", payload, &assignment); err != nil {
		return nil, fmt.Errorf("assign experiment %q: %w", experimentKey, err)
	}
	if assignment.ExperimentKey == "" {
		assignment.ExperimentKey = experimentKey
	}
	if assignment.UserID == "" {
		assignment.UserID = user.ID
	}

	c.cache.Set(key, &cachedAssignment{userID: user.ID, assignment: &assignment})
	return &assignment, nil
}

// Track implements Client.
func (c *client) Track(ctx context.Context, event *TrackEvent) error {
	if err := validateEvent(event); err != nil {
		return err
	}

	if event.hasKey() {
		if err := c.http.Post(ctx, "/api/v1/tracking/track", event.body(), nil); err != nil {
			return fmt.Errorf("track event %q: %w", event.EventName, err)
		}
		return nil
	}

	return c.sendBatch(ctx, c.fanOut(event))
}

// TrackBatch implements Client.
func (c *client) TrackBatch(ctx context.Context, events []*TrackEvent) error {
	bodies := make([]trackBody, 0, len(events))
	for _, event := range events {
		if err := validateEvent(event); err != nil {
			return err
		}
		if event.hasKey() {
			bodies = append(bodies, event.body())
		} else {
			bodies = append(bodies, c.fanOut(event)...)
		}
	}
	return c.sendBatch(ctx, bodies)
}

// Close implements Client.
func (c *client) Close() error {
	c.closeOnce.Do(func() {
		c.cache.Clear()
		c.http.CloseIdleConnections()
	})
	return nil
}

func validateEvent(event *TrackEvent) error {
	if event == nil {
		return errors.New("experimentation: event must not be nil")
	}
	if event.UserID == "" {
		return errors.New("experimentation: event.UserID must not be empty")
	}
	if event.EventName == "" && event.EventType == "" {
		return errors.New("experimentation: event.EventName must not be empty")
	}
	return nil
}

// fanOut expands an event without a key into one entry per cached assignment
// (experiment_key) plus one per cached evaluated flag (feature_flag_key) for
// the event's user. The result is empty when nothing is cached.
func (c *client) fanOut(event *TrackEvent) []trackBody {
	var experiments, flags []string
	c.cache.Range(func(_ string, value interface{}) {
		switch v := value.(type) {
		case *cachedAssignment:
			if v.userID == event.UserID {
				experiments = append(experiments, v.assignment.ExperimentKey)
			}
		case *cachedFlag:
			if v.userID == event.UserID {
				flags = append(flags, v.result.Key)
			}
		}
	})

	base := event.body()
	bodies := make([]trackBody, 0, len(experiments)+len(flags))
	for _, experimentKey := range experiments {
		b := base
		b.ExperimentKey = experimentKey
		bodies = append(bodies, b)
	}
	for _, flagKey := range flags {
		b := base
		b.FeatureFlagKey = flagKey
		bodies = append(bodies, b)
	}
	return bodies
}

// sendBatch posts bodies to /api/v1/tracking/batch in chunks of batchLimit.
// Nothing is sent when bodies is empty.
func (c *client) sendBatch(ctx context.Context, bodies []trackBody) error {
	for start := 0; start < len(bodies); start += batchLimit {
		end := start + batchLimit
		if end > len(bodies) {
			end = len(bodies)
		}
		payload := map[string]interface{}{"events": bodies[start:end]}
		if err := c.http.Post(ctx, "/api/v1/tracking/batch", payload, nil); err != nil {
			return fmt.Errorf("track batch: %w", err)
		}
	}
	return nil
}
