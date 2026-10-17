package experimentation

import "time"

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

// WithLocalEval is kept for source compatibility and has no effect.
//
// Deprecated: flags and experiments are evaluated by the server; the SDK no
// longer downloads flag definitions or buckets users locally.
func WithLocalEval(enabled bool) Option {
	return func(*SdkConfig) {}
}
