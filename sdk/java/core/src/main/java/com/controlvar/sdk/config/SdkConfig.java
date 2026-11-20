package com.controlvar.sdk.config;

import com.controlvar.sdk.ErrorListener;
import com.controlvar.sdk.ExposureListener;

/**
 * Configuration for the Controlvar SDK client.
 *
 * <p>Use the builder pattern to construct a config instance:
 * <pre>
 *     SdkConfig config = SdkConfig.builder("my-api-key", "https://api.example.com")
 *         .timeoutMs(3000)
 *         .cacheSize(500)
 *         .cacheTtlMs(60_000L)
 *         .build();
 * </pre>
 */
public class SdkConfig {
    private final String apiKey;
    private final String baseUrl;
    private final int timeoutMs;
    private final int cacheSize;
    private final long cacheTtlMs;
    private final ExposureListener exposureListener;
    private final ErrorListener errorListener;

    private SdkConfig(Builder builder) {
        this.apiKey = builder.apiKey;
        this.baseUrl = builder.baseUrl;
        this.timeoutMs = builder.timeoutMs;
        this.cacheSize = builder.cacheSize;
        this.cacheTtlMs = builder.cacheTtlMs;
        this.exposureListener = builder.exposureListener;
        this.errorListener = builder.errorListener;
    }

    /**
     * Returns the API key used to authenticate requests.
     */
    public String getApiKey() {
        return apiKey;
    }

    /**
     * Returns the base URL of the Controlvar API (no trailing slash).
     */
    public String getBaseUrl() {
        return baseUrl;
    }

    /**
     * Returns the HTTP connect/read timeout in milliseconds.
     */
    public int getTimeoutMs() {
        return timeoutMs;
    }

    /**
     * Returns the maximum number of entries in the assignment cache.
     */
    public int getCacheSize() {
        return cacheSize;
    }

    /**
     * Returns the TTL (time-to-live) for cache entries in milliseconds.
     */
    public long getCacheTtlMs() {
        return cacheTtlMs;
    }

    /**
     * Returns the function the client calls once for each experiment exposure the server
     * recorded (see {@link ExposureListener}).
     *
     * @return the listener, or {@code null} when none is set
     */
    public ExposureListener getExposureListener() {
        return exposureListener;
    }

    /**
     * Returns the function the client calls each time a typed getter returns the caller's
     * default (see {@link ErrorListener}).
     *
     * @return the listener, or {@code null} when none is set
     */
    public ErrorListener getErrorListener() {
        return errorListener;
    }

    /**
     * Creates a new builder for {@link SdkConfig}.
     *
     * @param apiKey  the API key (required, must be non-empty)
     * @param baseUrl the base URL of the API (required, must be non-empty)
     * @return a new Builder instance
     */
    public static Builder builder(String apiKey, String baseUrl) {
        return new Builder(apiKey, baseUrl);
    }

    /**
     * Builder for {@link SdkConfig}.
     */
    public static class Builder {
        private final String apiKey;
        private final String baseUrl;
        private int timeoutMs = 5000;
        private int cacheSize = 1000;
        private long cacheTtlMs = 300_000L; // 5 minutes
        private ExposureListener exposureListener;
        private ErrorListener errorListener;

        /**
         * Constructs a builder with the required fields.
         *
         * @param apiKey  API key for authentication
         * @param baseUrl base URL of the API server
         * @throws IllegalArgumentException if apiKey or baseUrl is null or empty
         */
        public Builder(String apiKey, String baseUrl) {
            if (apiKey == null || apiKey.isEmpty()) {
                throw new IllegalArgumentException("apiKey is required and must not be empty");
            }
            if (baseUrl == null || baseUrl.isEmpty()) {
                throw new IllegalArgumentException("baseUrl is required and must not be empty");
            }
            this.apiKey = apiKey;
            // Normalize: strip trailing slash
            this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        }

        /**
         * Sets the HTTP connect/read timeout in milliseconds. Default: 5000.
         *
         * @param ms timeout in milliseconds (must be positive)
         * @return this builder
         */
        public Builder timeoutMs(int ms) {
            if (ms <= 0) {
                throw new IllegalArgumentException("timeoutMs must be positive");
            }
            this.timeoutMs = ms;
            return this;
        }

        /**
         * Sets the maximum number of entries in the in-memory assignment cache. Default: 1000.
         *
         * @param size maximum cache size (must be positive)
         * @return this builder
         */
        public Builder cacheSize(int size) {
            if (size <= 0) {
                throw new IllegalArgumentException("cacheSize must be positive");
            }
            this.cacheSize = size;
            return this;
        }

        /**
         * Sets the cache TTL in milliseconds. Default: 300000 (5 minutes).
         *
         * @param ms TTL in milliseconds (must be positive)
         * @return this builder
         */
        public Builder cacheTtlMs(long ms) {
            if (ms <= 0) {
                throw new IllegalArgumentException("cacheTtlMs must be positive");
            }
            this.cacheTtlMs = ms;
            return this;
        }

        /**
         * Sets the function the client calls once each time the server enrols the user in an
         * experiment and the answer is not cached (see {@link ExposureListener}). Default: none.
         *
         * @param listener the listener, or {@code null} to set none
         * @return this builder
         */
        public Builder exposureListener(ExposureListener listener) {
            this.exposureListener = listener;
            return this;
        }

        /**
         * Sets the function the client calls, with the reason, each time a typed getter
         * ({@code getStringValue}, {@code getNumberValue}, {@code getJsonValue}) returns the
         * caller's default (see {@link ErrorListener}). Default: none.
         *
         * @param listener the listener, or {@code null} to set none
         * @return this builder
         */
        public Builder errorListener(ErrorListener listener) {
            this.errorListener = listener;
            return this;
        }

        /**
         * Builds and returns the {@link SdkConfig} instance.
         *
         * @return configured SdkConfig
         */
        public SdkConfig build() {
            return new SdkConfig(this);
        }
    }
}
