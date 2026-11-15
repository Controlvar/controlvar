package com.controlvar.sdk.spring;

import com.controlvar.sdk.ExperimentationClient;
import com.controlvar.sdk.ExposureListener;
import com.controlvar.sdk.config.SdkConfig;
import com.controlvar.sdk.model.Exposure;
import com.controlvar.sdk.model.User;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.NoUniqueBeanDefinitionException;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.lang.reflect.Method;
import java.lang.reflect.Parameter;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

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
 *   <li><b>Exposure listener</b> — one {@code ExposureListener} bean is wired into the
 *       client; two stop the application at startup.</li>
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

    // ── ExposureListener bean (#1499) ──────────────────────────────────────────
    //
    // These assign through the client bean against a local fake of POST
    // /api/v1/tracking/assign that answers "assigned": true, so "wired" means the listener
    // bean really received the exposure.

    @Test
    @DisplayName("one ExposureListener bean is the auto-configured client's listener: it receives the exposure once")
    void oneExposureListenerBeanIsWired() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key", "experimentation.base-url=" + FakeAssignApi.url())
                .withUserConfiguration(OneListenerConfiguration.class)
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    RecordingListener listener = (RecordingListener) context.getBean(ExposureListener.class);
                    ExperimentationClient client = context.getBean(ExperimentationClient.class);

                    client.getExperimentAssignment(User.builder("u-1").build(), "exp-1");
                    client.getExperimentAssignment(User.builder("u-1").build(), "exp-1");

                    assertThat(listener.received).containsExactly(new Exposure("exp-1", "u-1", "v-2", "treatment"));
                });
    }

    @Test
    @DisplayName("with no ExposureListener bean the client is created and assigns as before")
    void noExposureListenerBean() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key", "experimentation.base-url=" + FakeAssignApi.url())
                .run(context -> {
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    assertThat(context).doesNotHaveBean(ExposureListener.class);
                    assertThat(context.getBean(ExperimentationClient.class)
                            .getExperimentAssignment(User.builder("u-1").build(), "exp-1").getVariantName())
                            .isEqualTo("treatment");
                });
    }

    @Test
    @DisplayName("two ExposureListener beans stop the application at startup, even when one is named like the auto-configuration's parameter")
    void twoExposureListenerBeansFailAtStartup() {
        // One of the two beans is named "exposureListener", exactly the name of the
        // ObjectProvider<ExposureListener> parameter of
        // ExperimentationAutoConfiguration#experimentationClient. Compiled without -parameters
        // and without a LocalVariableTable (as now), Spring cannot see that name and getIfAvailable() refuses two beans. Compiled
        // with -parameters, Spring would pick the bean named like the parameter and silently
        // ignore the other, the context would start, and this test would fail.
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .withUserConfiguration(TwoListenersConfiguration.class)
                .run(context -> {
                    assertThat(context)
                            .as("two ExposureListener beans must stop the application. This context started:"
                                    + " either the starter was compiled with -parameters and Spring chose the bean"
                                    + " named exposureListener, or the listener is read with something other than"
                                    + " getIfAvailable(), such as getIfUnique(), which gives none for two")
                            .hasFailed();
                    // The message names both beans. Requiring the name "exposureListener" in it
                    // pins this test's bean to the parameter's name: a rename of the bean makes
                    // this test fail instead of silently losing what it guards.
                    assertThat(context.getStartupFailure())
                            .hasRootCauseInstanceOf(NoUniqueBeanDefinitionException.class)
                            .rootCause()
                            .hasMessageContaining(ExposureListener.class.getName())
                            .hasMessageMatching("(?s).*found 2: (exposureListener,otherExposureListener"
                                    + "|otherExposureListener,exposureListener)\\b.*");
                });
    }

    @Test
    @DisplayName("the auto-configuration is compiled without -parameters, which the two-bean failure depends on")
    void compiledWithoutParameterNames() throws NoSuchMethodException {
        Method factory = ExperimentationAutoConfiguration.class.getMethod(
                "experimentationClient", ExperimentationProperties.class, ObjectProvider.class);
        for (Parameter parameter : factory.getParameters()) {
            assertThat(parameter.isNamePresent())
                    .as("%s carries its name, so the starter was compiled with -parameters: Spring would then"
                            + " choose an ExposureListener bean named like it and ignore a second one", parameter)
                    .isFalse();
        }
    }

    @Test
    @DisplayName("the auto-configuration class carries no LocalVariableTable, which Spring Framework 6.0 reads parameter names from")
    void compiledWithoutLocalVariableTable() throws IOException {
        byte[] bytes;
        try (java.io.InputStream in = ExperimentationAutoConfiguration.class
                .getResourceAsStream("ExperimentationAutoConfiguration.class")) {
            assertThat(in).as("the class file of the auto-configuration").isNotNull();
            bytes = in.readAllBytes();
        }
        assertThat(new String(bytes, StandardCharsets.ISO_8859_1))
                .as("ExperimentationAutoConfiguration.class holds a LocalVariableTable (javac -g), so on"
                        + " Spring Boot 3.0 and 3.1 Spring would choose an ExposureListener bean named like"
                        + " the parameter and ignore a second one; the POM needs <debuglevel>lines,source")
                .doesNotContain("LocalVariableTable");
    }

    @Test
    @DisplayName("a user-defined ExperimentationClient bean wins, and does not get the ExposureListener bean")
    void userDefinedClientDoesNotGetTheListenerBean() {
        contextRunner
                .withPropertyValues("experimentation.api-key=test-key")
                .withUserConfiguration(OneListenerConfiguration.class, FakeApiClientConfiguration.class)
                .run(context -> {
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    assertThat(context).doesNotHaveBean("experimentationClient");
                    RecordingListener listener = (RecordingListener) context.getBean(ExposureListener.class);

                    assertThat(context.getBean(ExperimentationClient.class)
                            .getExperimentAssignment(User.builder("u-1").build(), "exp-1").getVariantName())
                            .isEqualTo("treatment");
                    assertThat(listener.received).isEmpty();
                });
    }

    @AfterAll
    static void stopFakeAssignApi() {
        FakeAssignApi.stop();
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

    /** An ExposureListener that records what it receives. */
    static final class RecordingListener implements ExposureListener {
        final List<Exposure> received = new CopyOnWriteArrayList<>();

        @Override
        public void onExposure(Exposure exposure) {
            received.add(exposure);
        }
    }

    @Configuration
    static class OneListenerConfiguration {

        @Bean
        ExposureListener recordingExposureListener() {
            return new RecordingListener();
        }
    }

    /** Two listener beans; the first is named exactly like the auto-configuration's parameter. */
    @Configuration
    static class TwoListenersConfiguration {

        @Bean
        ExposureListener exposureListener() {
            return new RecordingListener();
        }

        @Bean
        ExposureListener otherExposureListener() {
            return new RecordingListener();
        }
    }

    /** A user-defined client, pointed at the fake API, configured without a listener. */
    @Configuration
    static class FakeApiClientConfiguration {

        @Bean
        ExperimentationClient customExperimentationClient() {
            return new ExperimentationClient(SdkConfig.builder("custom-key", FakeAssignApi.url()).build());
        }
    }

    /**
     * A local fake of {@code POST /api/v1/tracking/assign} on an ephemeral 127.0.0.1 port (the
     * JDK's own HTTP server, so the starter gains no test dependency). It answers every request
     * as the server answers an enrolled user.
     */
    static final class FakeAssignApi {
        private static final String ASSIGNED = "{\"experiment_key\":\"exp-1\",\"user_id\":\"u-1\","
                + "\"variant_id\":\"v-2\",\"variant_name\":\"treatment\",\"is_control\":false,"
                + "\"configuration\":null,\"assigned\":true,\"reason\":\"assigned\"}";
        private static HttpServer server;

        static synchronized String url() {
            if (server == null) {
                try {
                    server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
                server.createContext("/api/v1/tracking/assign", exchange -> {
                    byte[] body = ASSIGNED.getBytes(StandardCharsets.UTF_8);
                    exchange.getResponseHeaders().set("Content-Type", "application/json");
                    exchange.sendResponseHeaders(200, body.length);
                    try (OutputStream out = exchange.getResponseBody()) {
                        out.write(body);
                    }
                });
                server.start();
            }
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        static synchronized void stop() {
            if (server != null) {
                server.stop(0);
                server = null;
            }
        }
    }
}
