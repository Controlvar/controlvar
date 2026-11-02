package com.controlvar.sdk.spring;

import com.controlvar.sdk.ExperimentationClient;
import com.controlvar.sdk.ExposureListener;
import com.controlvar.sdk.config.SdkConfig;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnClass;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;

/**
 * Spring Boot auto-configuration for the Controlvar SDK.
 *
 * <p>This class is registered as an auto-configuration entry via
 * {@code META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports}
 * and is activated automatically when:
 * <ol>
 *   <li>{@link ExperimentationClient} is on the classpath (i.e., the core SDK jar is present).</li>
 *   <li>The {@code experimentation.api-key} property is set in the application configuration.</li>
 * </ol>
 *
 * <h2>Minimal setup</h2>
 * Add the starter to your {@code pom.xml}:
 * <pre>{@code
 * <dependency>
 *     <groupId>com.controlvar</groupId>
 *     <artifactId>controlvar-spring-boot-starter</artifactId>
 *     <version>0.1.0</version>
 * </dependency>
 * }</pre>
 *
 * Then set the API key:
 * <pre>
 * # application.properties
 * experimentation.api-key=my-secret-api-key
 * </pre>
 *
 * <p>An {@link ExperimentationClient} bean is automatically registered and can be
 * injected via {@code @Autowired} or constructor injection.
 *
 * <h2>Custom bean override</h2>
 * If you declare your own {@link ExperimentationClient} bean, the auto-configured one
 * is suppressed (thanks to {@link ConditionalOnMissingBean}):
 * <pre>{@code
 * @Bean
 * public ExperimentationClient myExperimentationClient() {
 *     SdkConfig config = SdkConfig.builder("key", "https://my-host.internal")
 *         .timeoutMs(2000)
 *         .build();
 *     return new ExperimentationClient(config);
 * }
 * }</pre>
 *
 * <h2>Exposure listener</h2>
 * Declare one {@link ExposureListener} bean, and the auto-configured client calls it once
 * each time the server enrols a user in an experiment and the answer is not cached. Two
 * such beans stop the application at startup. A client bean of your own does not get the
 * listener bean: set it on your {@code SdkConfig} with {@code exposureListener(...)}.
 *
 * @see ExperimentationProperties
 * @see EnableExperimentation
 */
@AutoConfiguration
@ConditionalOnClass(ExperimentationClient.class)
@EnableConfigurationProperties(ExperimentationProperties.class)
@ConditionalOnProperty(prefix = "experimentation", name = "api-key")
public class ExperimentationAutoConfiguration {

    /**
     * Creates and registers an {@link ExperimentationClient} bean using properties
     * bound from the {@code experimentation.*} namespace.
     *
     * <p>The bean is only created when no other {@link ExperimentationClient} bean
     * exists in the application context ({@link ConditionalOnMissingBean}).
     *
     * <p>An {@link ExposureListener} bean, when there is one, becomes the client's exposure
     * listener. Two {@code ExposureListener} beans stop the application at startup with
     * {@code NoUniqueBeanDefinitionException}.
     *
     * @param properties       the bound configuration properties
     * @param exposureListener the application's {@link ExposureListener} bean, if it has one
     * @return a fully configured {@link ExperimentationClient} ready for injection
     */
    @Bean
    @ConditionalOnMissingBean
    public ExperimentationClient experimentationClient(ExperimentationProperties properties,
                                                       ObjectProvider<ExposureListener> exposureListener) {
        SdkConfig config = SdkConfig.builder(properties.getApiKey(), properties.getBaseUrl())
                .timeoutMs(properties.getTimeoutMs())
                .cacheSize(properties.getCacheSize())
                // SdkConfig.Builder uses milliseconds; properties expose seconds for user-friendliness
                .cacheTtlMs((long) properties.getCacheTtlSeconds() * 1000L)
                // getIfAvailable() throws NoUniqueBeanDefinitionException for two beans, which
                // stops startup; getIfUnique() would silently give none. That holds while this
                // module is compiled without -parameters: with them, Spring would pick the bean
                // named like this parameter ("exposureListener") and ignore the other.
                // ExperimentationAutoConfigurationTest pins it.
                .exposureListener(exposureListener.getIfAvailable())
                .build();
        return new ExperimentationClient(config);
    }
}
