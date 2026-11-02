package experimentation

import (
	"context"
	"time"
)

// SdkConfig holds all configuration for the SDK client.
type SdkConfig struct {
	// BaseURL is the origin of the Controlvar API, e.g.
	// "https://api.example.com". The SDK appends "/api/v1/...".
	BaseURL string
	// APIKey is sent as the X-API-Key header on every request.
	APIKey string
	// Timeout is the HTTP request timeout.
	Timeout time.Duration
	// CacheSize is the maximum number of cached evaluations + assignments.
	CacheSize int
	// CacheTTL is how long a successful evaluation or assignment is reused.
	CacheTTL time.Duration

	// onExposure is set by WithOnExposure. It points to the function rather
	// than holding it, because a func field would make SdkConfig no longer
	// comparable, which is an incompatible change to its API.
	onExposure *exposureHook
}

// exposureHook holds the function given to WithOnExposure.
type exposureHook struct {
	fn func(ctx context.Context, e Exposure)
}

// Option is a functional option for configuring an SdkConfig.
type Option func(*SdkConfig)

// defaultConfig returns an SdkConfig populated with sensible defaults.
func defaultConfig() *SdkConfig {
	return &SdkConfig{
		BaseURL:   "http://localhost:8000",
		Timeout:   10 * time.Second,
		CacheSize: 1000,
		CacheTTL:  5 * time.Minute,
	}
}

// WithBaseURL sets the origin of the Controlvar API.
func WithBaseURL(url string) Option {
	return func(c *SdkConfig) {
		c.BaseURL = url
	}
}

// WithAPIKey sets the API key sent as X-API-Key.
func WithAPIKey(key string) Option {
	return func(c *SdkConfig) {
		c.APIKey = key
	}
}

// WithTimeout sets the HTTP request timeout duration.
func WithTimeout(d time.Duration) Option {
	return func(c *SdkConfig) {
		c.Timeout = d
	}
}

// WithCacheSize sets the maximum number of evaluations and assignments to cache.
func WithCacheSize(size int) Option {
	return func(c *SdkConfig) {
		c.CacheSize = size
	}
}

// WithCacheTTL sets how long a successful evaluation or assignment is reused
// (default 5 minutes). Failures are never cached.
func WithCacheTTL(d time.Duration) Option {
	return func(c *SdkConfig) {
		c.CacheTTL = d
	}
}

// WithOnExposure sets a function the client calls once each time the server
// enrols a user in an experiment and the answer did not come from the
// client's cache: when GetAssignment stores an answer of
// POST /api/v1/tracking/assign that says "assigned": true, for a user and
// experiment with no unexpired cached answer. It is not called for a cached
// answer, an answer whose "assigned" is false or missing, a failed call, or a
// flag evaluation.
//
// fn runs on the goroutine that called GetAssignment, after the answer is
// cached and before GetAssignment returns, with no lock of the client held.
// ctx is the context GetAssignment was given; its deadline does not bound fn.
// fn can run on several goroutines at once. If fn panics, the client
// recovers: GetAssignment returns the answer and a nil error, and nothing
// records the panic. WithOnExposure(nil) sets none.
func WithOnExposure(fn func(ctx context.Context, e Exposure)) Option {
	return func(c *SdkConfig) {
		if fn == nil {
			c.onExposure = nil
			return
		}
		c.onExposure = &exposureHook{fn: fn}
	}
}

// WithLocalEval is kept for source compatibility and has no effect.
//
// Deprecated: flags and experiments are evaluated by the server; the SDK no
// longer downloads flag definitions or buckets users locally.
func WithLocalEval(enabled bool) Option {
	return func(*SdkConfig) {}
}
