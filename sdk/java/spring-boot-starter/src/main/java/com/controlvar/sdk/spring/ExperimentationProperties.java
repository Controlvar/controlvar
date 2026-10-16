package com.controlvar.sdk.spring;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Configuration properties for the Controlvar SDK, bound from the
 * {@code experimentation.*} namespace in {@code application.properties} /
 * {@code application.yml}.
 *
 * <h2>Minimal configuration</h2>
 * <pre>
 * # application.properties
 * experimentation.api-key=my-secret-api-key
 * </pre>
 *
 * <h2>Full configuration</h2>
 * <pre>
 * experimentation.api-key=my-secret-api-key
 * experimentation.base-url=https://api.controlvar.example.com
 * experimentation.cache-ttl-seconds=300
 * experimentation.cache-size=1000
 * experimentation.timeout-ms=5000
 * </pre>
 *
 * <p>All properties support Spring Boot's relaxed binding:
 * {@code api-key}, {@code apiKey}, {@code API_KEY}, and {@code EXPERIMENTATION_API_KEY}
 * environment variables are all equivalent.
 */
@ConfigurationProperties(prefix = "experimentation")
public class ExperimentationProperties {

    /**
     * API key used to authenticate SDK requests to Controlvar.
     * Required — auto-configuration is disabled when this is absent.
     */
    private String apiKey;

    /**
     * Base URL of the Controlvar API (no trailing slash).
     * Default: {@code https://api.controlvar.example.com}
     */
    private String baseUrl = "https://api.controlvar.example.com";

    /**
     * TTL for cached flag-evaluation results, in seconds.
     * Default: {@code 300} (5 minutes).
     */
    private int cacheTtlSeconds = 300;

    /**
     * Maximum number of entries the in-memory assignment cache may hold before
     * LRU eviction kicks in.
     * Default: {@code 1000}.
     */
    private int cacheSize = 1000;

    /**
     * HTTP connect/read/write timeout in milliseconds.
     * Default: {@code 5000} (5 seconds).
     */
    private int timeoutMs = 5000;

    // ── Getters ────────────────────────────────────────────────────────────────

    /**
     * Returns the API key.
     *
     * @return API key, or {@code null} if not configured
     */
    public String getApiKey() {
        return apiKey;
    }

    /**
     * Returns the base URL of the API.
     *
     * @return base URL (never {@code null} — defaults to the canonical platform URL)
     */
    public String getBaseUrl() {
        return baseUrl;
    }

    /**
     * Returns the cache TTL in seconds.
     *
     * @return TTL in seconds (default {@code 300})
     */
    public int getCacheTtlSeconds() {
        return cacheTtlSeconds;
    }

    /**
     * Returns the maximum cache size (number of entries).
     *
     * @return max entries (default {@code 1000})
     */
    public int getCacheSize() {
        return cacheSize;
    }

    /**
     * Returns the HTTP timeout in milliseconds.
     *
     * @return timeout ms (default {@code 5000})
     */
    public int getTimeoutMs() {
        return timeoutMs;
    }

    // ── Setters ────────────────────────────────────────────────────────────────

    /**
     * Sets the API key.
     *
     * @param apiKey API key (required)
     */
    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    /**
     * Sets the base URL of the API.
     *
     * @param baseUrl base URL (must not be null or empty)
     */
    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    /**
     * Sets the cache TTL.
     *
     * @param cacheTtlSeconds TTL in seconds (must be positive)
     */
    public void setCacheTtlSeconds(int cacheTtlSeconds) {
        this.cacheTtlSeconds = cacheTtlSeconds;
    }

    /**
     * Sets the maximum cache size.
     *
     * @param cacheSize max number of entries (must be positive)
     */
    public void setCacheSize(int cacheSize) {
        this.cacheSize = cacheSize;
    }

    /**
     * Sets the HTTP timeout.
     *
     * @param timeoutMs timeout in milliseconds (must be positive)
     */
    public void setTimeoutMs(int timeoutMs) {
        this.timeoutMs = timeoutMs;
    }
}
