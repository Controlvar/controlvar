package com.controlvar.sdk.spring;

import com.controlvar.sdk.ExperimentationClient;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.context.annotation.ImportCandidates;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.util.ClassUtils;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Tests that the starter is actually <em>registered</em> as an auto-configuration.
 *
 * <p>{@link ExperimentationAutoConfigurationTest} hands the class to
 * {@code AutoConfigurations.of(ExperimentationAutoConfiguration.class)} by name, which
 * bypasses the registration file entirely: every one of its tests passes with
 * {@code META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports}
 * deleted, renamed or pointing at a class that does not exist — i.e. with the starter
 * doing nothing at all in a real Spring Boot application. The tests here close that hole
 * by going through Spring Boot's own discovery ({@link ImportCandidates} and the
 * classpath resource it reads), so a broken or missing registration fails the build.
 */
@DisplayName("Experimentation starter registration")
class ExperimentationStarterRegistrationTest {

    private static final String IMPORTS_RESOURCE =
            "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports";

    private static final String AUTO_CONFIGURATION_CLASS =
            "com.controlvar.sdk.spring.ExperimentationAutoConfiguration";

    private static final ClassLoader CLASS_LOADER =
            ExperimentationAutoConfiguration.class.getClassLoader();

    /**
     * Class names listed in <em>every</em> {@value #IMPORTS_RESOURCE} on the classpath.
     * There are many — spring-boot-autoconfigure ships its own — so callers filter.
     */
    private static List<String> registeredClassNames() throws IOException {
        List<String> names = new ArrayList<>();
        for (URL url : Collections.list(CLASS_LOADER.getResources(IMPORTS_RESOURCE))) {
            try (InputStream in = url.openStream();
                 BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
                reader.lines()
                        .map(String::trim)
                        .filter(line -> !line.isEmpty() && !line.startsWith("#"))
                        .forEach(names::add);
            }
        }
        return names;
    }

    /** The starter's own entries: everything it contributes to auto-configuration. */
    private static List<String> ourRegisteredClassNames() throws IOException {
        List<String> ours = new ArrayList<>();
        for (String name : registeredClassNames()) {
            if (name.startsWith("com.controlvar.")) {
                ours.add(name);
            }
        }
        return ours;
    }

    @Test
    @DisplayName("the AutoConfiguration.imports resource is on the classpath and names the auto-configuration")
    void importsResourceNamesTheAutoConfiguration() throws IOException {
        assertThat(Collections.list(CLASS_LOADER.getResources(IMPORTS_RESOURCE)))
                .as("%s must be packaged with the starter — without it Spring Boot never "
                        + "sees the auto-configuration", IMPORTS_RESOURCE)
                .isNotEmpty();

        assertThat(registeredClassNames()).contains(AUTO_CONFIGURATION_CLASS);
    }

    @Test
    @DisplayName("Spring Boot's own discovery finds the auto-configuration")
    void springBootDiscoversTheAutoConfiguration() {
        List<String> candidates = new ArrayList<>();
        ImportCandidates.load(AutoConfiguration.class, CLASS_LOADER).forEach(candidates::add);

        assertThat(candidates)
                .as("ImportCandidates is what Spring Boot uses at startup; the class must be there")
                .contains(AUTO_CONFIGURATION_CLASS);
    }

    @Test
    @DisplayName("every class the starter registers exists and is loadable")
    void everyRegisteredClassIsLoadable() throws IOException {
        List<String> ours = ourRegisteredClassNames();
        assertThat(ours).isNotEmpty();
        for (String name : ours) {
            assertThat(ClassUtils.isPresent(name, CLASS_LOADER))
                    .as("registered auto-configuration '%s' cannot be loaded — a typo or a "
                            + "rename would silently disable the starter", name)
                    .isTrue();
            assertDoesNotThrow(() -> Class.forName(name, false, CLASS_LOADER));
        }
    }

    @Test
    @DisplayName("the auto-configuration carries @AutoConfiguration")
    void theClassIsAnnotated() {
        assertThat(ExperimentationAutoConfiguration.class.getAnnotation(AutoConfiguration.class))
                .as("@AutoConfiguration is what makes the imports entry meaningful")
                .isNotNull();
    }

    @Test
    @DisplayName("a context built from the registered entries alone gets an ExperimentationClient")
    void contextFromRegisteredEntriesCreatesTheClient() throws Exception {
        List<Class<?>> registered = new ArrayList<>();
        for (String name : ourRegisteredClassNames()) {
            registered.add(Class.forName(name, false, CLASS_LOADER));
        }
        assertThat(registered).isNotEmpty();

        // Unlike the sibling test class, nothing here names ExperimentationAutoConfiguration:
        // the configurations come from what Spring Boot discovered on the classpath.
        new ApplicationContextRunner()
                .withConfiguration(AutoConfigurations.of(registered.toArray(new Class<?>[0])))
                .withPropertyValues("experimentation.api-key=test-key")
                .run(context -> {
                    assertThat(context).hasNotFailed();
                    assertThat(context).hasSingleBean(ExperimentationClient.class);
                    assertThat(context).hasSingleBean(ExperimentationProperties.class);
                });
    }
}
