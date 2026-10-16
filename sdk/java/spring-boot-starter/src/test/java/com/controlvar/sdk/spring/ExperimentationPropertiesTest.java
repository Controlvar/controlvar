package com.controlvar.sdk.spring;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link ExperimentationProperties}.
 *
 * <p>These tests verify the default values, setters, and basic contract of the
 * properties class in isolation — without requiring a Spring application context.
 */
@DisplayName("ExperimentationProperties")
class ExperimentationPropertiesTest {

    private ExperimentationProperties properties;

    @BeforeEach
    void setUp() {
        properties = new ExperimentationProperties();
    }

    // ── Default values ─────────────────────────────────────────────────────────

    @Test
    @DisplayName("default baseUrl starts with https://")
    void defaultBaseUrlStartsWithHttps() {
        assertTrue(
            properties.getBaseUrl().startsWith("https://"),
            "Default baseUrl must be an HTTPS URL, got: " + properties.getBaseUrl()
        );
    }

    @Test
    @DisplayName("default baseUrl is the canonical platform URL")
    void defaultBaseUrlIsCanonicalPlatformUrl() {
        assertEquals(
            "https://api.controlvar.example.com",
            properties.getBaseUrl()
        );
    }

    @Test
    @DisplayName("default cacheTtlSeconds is 300 (5 minutes)")
    void defaultCacheTtlIs300() {
        assertEquals(300, properties.getCacheTtlSeconds());
    }

    @Test
    @DisplayName("default cacheSize is 1000")
    void defaultCacheSizeIs1000() {
        assertEquals(1000, properties.getCacheSize());
    }

    @Test
    @DisplayName("default timeoutMs is 5000")
    void defaultTimeoutIs5000() {
        assertEquals(5000, properties.getTimeoutMs());
    }

    @Test
    @DisplayName("apiKey is null by default")
    void apiKeyIsNullByDefault() {
        assertNull(properties.getApiKey(), "apiKey should be null when not configured");
    }

    // ── Setters ────────────────────────────────────────────────────────────────

    @Test
    @DisplayName("setApiKey stores and returns the value")
    void setApiKeyWorks() {
        properties.setApiKey("test-key-12345");
        assertEquals("test-key-12345", properties.getApiKey());
    }

    @Test
    @DisplayName("setBaseUrl stores and returns the value")
    void setBaseUrlWorks() {
        properties.setBaseUrl("https://custom-host.internal");
        assertEquals("https://custom-host.internal", properties.getBaseUrl());
    }

    @Test
    @DisplayName("setCacheTtlSeconds stores and returns the value")
    void setCacheTtlSecondsWorks() {
        properties.setCacheTtlSeconds(600);
        assertEquals(600, properties.getCacheTtlSeconds());
    }

    @Test
    @DisplayName("setCacheSize stores and returns the value")
    void setCacheSizeWorks() {
        properties.setCacheSize(500);
        assertEquals(500, properties.getCacheSize());
    }

    @Test
    @DisplayName("setTimeoutMs stores and returns the value")
    void setTimeoutMsWorks() {
        properties.setTimeoutMs(3000);
        assertEquals(3000, properties.getTimeoutMs());
    }

    // ── Edge cases ─────────────────────────────────────────────────────────────

    @Test
    @DisplayName("all setters are independent — setting one does not affect others")
    void settersAreIndependent() {
        properties.setApiKey("my-key");
        // All other defaults should still be intact
        assertEquals("https://api.controlvar.example.com", properties.getBaseUrl());
        assertEquals(300, properties.getCacheTtlSeconds());
        assertEquals(1000, properties.getCacheSize());
        assertEquals(5000, properties.getTimeoutMs());
    }
}
