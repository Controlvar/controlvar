package com.controlvar.sdk.spring;

import org.springframework.context.annotation.Import;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Enables Controlvar SDK auto-configuration when placed on a
 * {@code @Configuration} class or a Spring Boot {@code @SpringBootApplication} class.
 *
 * <p>Using this annotation is <em>optional</em> — Spring Boot's standard
 * auto-configuration mechanism (via
 * {@code META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports})
 * activates {@link ExperimentationAutoConfiguration} automatically whenever
 * the SDK is on the classpath and {@code experimentation.api-key} is configured.
 *
 * <p>Use {@code @EnableExperimentation} explicitly when:
 * <ul>
 *   <li>You are not using Spring Boot (plain Spring Framework) and rely on
 *       {@code @Import}-based configuration.</li>
 *   <li>You want to make the SDK dependency visible / self-documenting in your
 *       application's primary configuration class.</li>
 * </ul>
 *
 * <h2>Usage example</h2>
 * <pre>{@code
 * @SpringBootApplication
 * @EnableExperimentation
 * public class MyApplication {
 *     public static void main(String[] args) {
 *         SpringApplication.run(MyApplication.class, args);
 *     }
 * }
 * }</pre>
 *
 * @see ExperimentationAutoConfiguration
 * @see ExperimentationProperties
 */
@Target(ElementType.TYPE)
@Retention(RetentionPolicy.RUNTIME)
@Documented
@Import(ExperimentationAutoConfiguration.class)
public @interface EnableExperimentation {
}
