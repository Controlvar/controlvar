package experimentation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// HTTPClient wraps net/http.Client with context support, JSON encoding/decoding,
// and automatic API key authentication via the X-API-Key header.
type HTTPClient struct {
	client  *http.Client
	baseURL string
	apiKey  string
}

// NewHTTPClient creates an HTTPClient configured with the given base URL, API key,
// and request timeout.
func NewHTTPClient(baseURL, apiKey string, timeout time.Duration) *HTTPClient {
	return &HTTPClient{
		client: &http.Client{
			Timeout: timeout,
		},
		baseURL: baseURL,
		apiKey:  apiKey,
	}
}

// Get performs a GET request to path and decodes the JSON response body into result.
// The request is cancelled if ctx is cancelled before the response arrives.
func (h *HTTPClient) Get(ctx context.Context, path string, result interface{}) error {
	req, err := h.buildRequest(ctx, http.MethodGet, path, nil)
	if err != nil {
		return fmt.Errorf("build GET request: %w", err)
	}
	return h.do(req, result)
}

// Post performs a POST request to path, encoding body as JSON, and decodes the
// JSON response into result (skipped when result is nil). The request is
// cancelled if ctx is cancelled.
func (h *HTTPClient) Post(ctx context.Context, path string, body interface{}, result interface{}) error {
	var encoded []byte
	if body != nil {
		var err error
		encoded, err = json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal request body: %w", err)
		}
	}

	req, err := h.buildRequest(ctx, http.MethodPost, path, encoded)
	if err != nil {
		return fmt.Errorf("build POST request: %w", err)
	}
	return h.do(req, result)
}

// CloseIdleConnections closes any idle connections in the underlying pool.
func (h *HTTPClient) CloseIdleConnections() {
	h.client.CloseIdleConnections()
}

// buildRequest creates an *http.Request for the given method and path, attaching
// the API key and JSON headers and (optionally) the JSON request body.
func (h *HTTPClient) buildRequest(ctx context.Context, method, path string, body []byte) (*http.Request, error) {
	url := h.baseURL + path

	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}

	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, err
	}

	if h.apiKey != "" {
		req.Header.Set("X-API-Key", h.apiKey)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/json")
	return req, nil
}

// do executes the request and decodes the JSON response body into result.
// Returns an error for non-2xx status codes or decode failures.
func (h *HTTPClient) do(req *http.Request, result interface{}) error {
	resp, err := h.client.Do(req)
	if err != nil {
		return fmt.Errorf("http request failed: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return &APIError{
			StatusCode: resp.StatusCode,
			Message:    string(body),
			RetryAfter: resp.Header.Get("Retry-After"),
		}
	}

	if result != nil {
		if err := json.NewDecoder(resp.Body).Decode(result); err != nil {
			return fmt.Errorf("decode response body: %w", err)
		}
	}
	return nil
}

// APIError is returned when the server responds with a non-2xx HTTP status code:
// 401 bad API key, 404 an unknown flag key, or an experiment that is unknown or not ACTIVE, 422 invalid event,
// 429 rate limited (see RetryAfter).
type APIError struct {
	StatusCode int
	Message    string
	// RetryAfter is the raw Retry-After header, set on 429 responses.
	RetryAfter string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("API error %d: %s", e.StatusCode, e.Message)
}
