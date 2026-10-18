package com.controlvar.sdk.spring;

import com.controlvar.sdk.ExperimentationClient;
import com.controlvar.sdk.config.SdkConfig;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Integration tests for {@link ExperimentationAutoConfiguration} using
 * {@link ApplicationContextRunner}.
 *
 * <p>Each test spins up a lightweight Spring application context with only the
 * auto-configuration under test (no full Spring Boot context). This makes the tests
 * fast and focused.
 *
 * <p>Test categories:
 * <ul>
 *   <li><b>Activation conditions</b> — verifies the {@code @ConditionalOn*} guards.</li>
 *   <li><b>Property binding</b> — verifies that {@code experimentation.*} properties
 *       are correctly wired into the created bean.</li>
 *   <li><b>Custom bean override</b> — verifies {@code @ConditionalOnMissingBean}
 *       behaviour: a user-defined bean suppresses the auto-configured one.</li>
 * </ul>
 */
@DisplayName("ExperimentationAutoConfiguration")
class ExperimentationAutoConfigurationTest {

    /**
     * Shared context runner with the auto-configuration registered.
     * Individual tests add properties and additional configurations as needed.
     */
    private final ApplicationContextRunner contextRunner = new ApplicationContextRunner()
            .withConfiguration(AutoConfigurations.of(ExperimentationAutoConfiguration.class));

    // ── Activation: no api-key ─────────────────────────────────────────────────

    @Test
    @DisplayName("no beans are created when experimentation.api-key is absent")
    void beansNotCreatedWithoutApiKey() {
        contextRunner.run(context -> {
            assertThat(context).doesNotHaveBean(ExperimentationClient.class);
            assertThat(context).doesNotHaveBean(ExperimentationAutoConfiguration.class);
        });
    }

    @Test
    @DisplayName("context fails to start when experimentation.api-key is empty string (SdkConfig rejects it)")
    void beansNotCreatedWhenApiKeyIsEmpty() {
        // @ConditionalOnProperty considers an empty string as "property is present", so
        // the auto-configuration activates. SdkConfig.Builder then throws because apiKey
        // must not be empty — resulting in a context startup failure rather than a missing bean.
        contextRunner
                .withPropertyValues("experimentation.api-key=")
                .run(context -> assertThat(context).hasFailed());
    }

    // ── Activation: with api-key ───────────────────────────────────────────────

    @Test
    @DisplayName("ExperimentationClient bean is created when api-key is present")
    void beansCreatedWhenApiKeyPresent() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    assertThat(context).hasSingleBean(ExperimentationProperties.class);
                });
    }

    @Test
    @DisplayName("created bean is of the correct type ExperimentationClient")
    void clientIsOfCorrectType() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    ExperimentationClient client = context.getBean(ExperimentationClient.class);
                    assertThat(client).isNotNull();
                    assertThat(client).isInstanceOf(ExperimentationClient.class);
                });
    }

    // ── Default property values ────────────────────────────────────────────────

    @Test
    @DisplayName("default baseUrl is used when not overridden")
    void defaultBaseUrlIsUsed() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getBaseUrl())
                            .isEqualTo("https://api.controlvar.example.com");
                });
    }

    @Test
    @DisplayName("default cacheTtlSeconds is 300 when not overridden")
    void defaultCacheTtlIsUsed() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getCacheTtlSeconds()).isEqualTo(300);
                });
    }

    @Test
    @DisplayName("default cacheSize is 1000 when not overridden")
    void defaultCacheSizeIsUsed() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getCacheSize()).isEqualTo(1000);
                });
    }

    @Test
    @DisplayName("default timeoutMs is 5000 when not overridden")
    void defaultTimeoutIsUsed() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getTimeoutMs()).isEqualTo(5000);
                });
    }

    // ── Custom property values ─────────────────────────────────────────────────

    @Test
    @DisplayName("custom baseUrl is used when configured")
    void customBaseUrlIsUsed() {
        contextRunner
                .withPropertyValues(
                        "experimentation.api-key=test-key",
                        "experimentation.base-url=https://api.internal.example.com"
                )
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getBaseUrl()).isEqualTo("https://api.internal.example.com");
                });
    }

    @Test
    @DisplayName("cacheTtlSeconds is configurable via experimentation.cache-ttl-seconds")
    void cacheTtlIsConfigurable() {
        contextRunner
                .withPropertyValues(
                        "experimentation.api-key=test-key",
                        "experimentation.cache-ttl-seconds=60"
                )
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getCacheTtlSeconds()).isEqualTo(60);
                });
    }

    @Test
    @DisplayName("cacheSize is configurable via experimentation.cache-size")
    void cacheSizeIsConfigurable() {
        contextRunner
                .withPropertyValues(
                        "experimentation.api-key=test-key",
                        "experimentation.cache-size=250"
                )
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getCacheSize()).isEqualTo(250);
                });
    }

    @Test
    @DisplayName("timeoutMs is configurable via experimentation.timeout-ms")
    void timeoutIsConfigurable() {
        contextRunner
                .withPropertyValues(
                        "experimentation.api-key=test-key",
                        "experimentation.timeout-ms=2000"
                )
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getTimeoutMs()).isEqualTo(2000);
                });
    }

    @Test
    @DisplayName("all custom properties are bound together correctly")
    void propertiesAreBound() {
        contextRunner
                .withPropertyValues(
                        "experimentation.api-key=my-api-key",
                        "experimentation.base-url=https://custom.example.com",
                        "experimentation.cache-ttl-seconds=120",
                        "experimentation.cache-size=500",
                        "experimentation.timeout-ms=3000"
                )
                .run(context -> {
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getApiKey()).isEqualTo("my-api-key");
                    assertThat(props.getBaseUrl()).isEqualTo("https://custom.example.com");
                    assertThat(props.getCacheTtlSeconds()).isEqualTo(120);
                    assertThat(props.getCacheSize()).isEqualTo(500);
                    assertThat(props.getTimeoutMs()).isEqualTo(3000);
                });
    }

    // ── Relaxed binding ────────────────────────────────────────────────────────

    @Test
    @DisplayName("api-key with kebab-case property name is accepted (relaxed binding)")
    void apiKeyWithKebabCaseIsAccepted() {
        contextRunner
                .withPropertyValues("experimentation.api-key=kebab-case-key")
                .run(context -> {
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getApiKey()).isEqualTo("kebab-case-key");
                });
    }

    @Test
    @DisplayName("api-key with underscores in value (e.g. service_account_key) is accepted")
    void apiKeyWithUnderscoresInValueIsAccepted() {
        contextRunner
                .withPropertyValues("experimentation.api-key=service_account_key_abc123")
                .run(context -> {
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    ExperimentationProperties props = context.getBean(ExperimentationProperties.class);
                    assertThat(props.getApiKey()).isEqualTo("service_account_key_abc123");
                });
    }

    // ── @ConditionalOnMissingBean ──────────────────────────────────────────────

    @Test
    @DisplayName("user-defined ExperimentationClient bean overrides the auto-configured one")
    void conditionalOnMissingBeanRespected() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .withUserConfiguration(CustomClientConfiguration.class)
                .run(context -> {
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    // The bean should be the custom one (named "customExperimentationClient")
                    assertThat(context).hasBean("customExperimentationClient");
                    assertThat(context).doesNotHaveBean("experimentationClient");
                });
    }

    // ── ConditionalOnClass (simulated via classpath presence check) ───────────

    @Test
    @DisplayName("auto-configuration activates because ExperimentationClient is on the classpath")
    void conditionalOnClassSatisfiedBecauseSdkIsPresent() {
        // If the class were missing, the context would fail to load — so this test
        // confirms the SDK jar is properly on the classpath.
        assertThat(ExperimentationClient.class).isNotNull();

        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> assertThat(context).hasSingleBean(ExperimentationClient.class));
    }

    // ── Multiple beans protection ──────────────────────────────────────────────

    @Test
    @DisplayName("exactly one ExperimentationClient bean is registered (no duplicates)")
    void exactlyOneClientBeanIsRegistered() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    String[] beanNames = context.getBeanNamesForType(ExperimentationClient.class);
                    assertThat(beanNames).hasSize(1);
                });
    }

    // ── Helper configuration classes ──────────────────────────────────────────

    /**
     * A user-supplied configuration that registers a custom {@link ExperimentationClient}.
     * Used to test that {@code @ConditionalOnMissingBean} suppresses the auto-configured bean.
     */
    @Configuration
    static class CustomClientConfiguration {

        @Bean
        ExperimentationClient customExperimentationClient() {
            SdkConfig config = SdkConfig.builder("custom-key", "https://custom.internal.example.com")
                    .timeoutMs(1000)
                    .build();
            return new ExperimentationClient(config);
        }
    }
}
