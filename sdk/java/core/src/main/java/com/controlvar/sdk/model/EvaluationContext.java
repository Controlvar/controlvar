package com.controlvar.sdk.model;

import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;

/**
 * Combines a {@link User} with optional environment metadata for flag evaluation.
 *
 * <p>Environment attributes (e.g., "environment", "appVersion", "platform") can
 * influence targeting rules beyond user-level attributes.
 *
 * <pre>
 *     EvaluationContext ctx = EvaluationContext.builder(user)
 *         .environmentAttribute("environment", "production")
 *         .environmentAttribute("appVersion", "2.3.1")
 *         .build();
 * </pre>
 */
public class EvaluationContext {

    private final User user;
    private final Map<String, Object> environmentAttributes;

    private EvaluationContext(Builder builder) {
        this.user = Objects.requireNonNull(builder.user, "user must not be null");
        this.environmentAttributes =
                Collections.unmodifiableMap(new HashMap<>(builder.environmentAttributes));
    }

    /**
     * Returns the user associated with this evaluation context.
     *
     * @return non-null User
     */
    public User getUser() {
        return user;
    }

    /**
     * Returns an unmodifiable view of the environment attributes.
     *
     * @return map of environment attribute key-value pairs
     */
    public Map<String, Object> getEnvironmentAttributes() {
        return environmentAttributes;
    }

    /**
     * Returns the value of a specific environment attribute, or null if not present.
     *
     * @param key attribute key
     * @return attribute value or null
     */
    public Object getEnvironmentAttribute(String key) {
        return environmentAttributes.get(key);
    }

    /**
     * Creates a new builder for {@link EvaluationContext}.
     *
     * @param user the user to evaluate for (must not be null)
     * @return new Builder
     */
    public static Builder builder(User user) {
        return new Builder(user);
    }

    @Override
    public String toString() {
        return "EvaluationContext{user=" + user +
               ", environmentAttributes=" + environmentAttributes + "}";
    }

    /**
     * Builder for {@link EvaluationContext}.
     */
    public static class Builder {
        private final User user;
        private final Map<String, Object> environmentAttributes = new HashMap<>();

        /**
         * @param user the user for this context (must not be null)
         */
        public Builder(User user) {
            this.user = Objects.requireNonNull(user, "user must not be null");
        }

        /**
         * Adds an environment-level attribute.
         *
         * @param key   attribute name (e.g., "environment", "platform")
         * @param value attribute value
         * @return this builder
         */
        public Builder environmentAttribute(String key, Object value) {
            if (key == null || key.isEmpty()) {
                throw new IllegalArgumentException("Environment attribute key must not be null or empty");
            }
            environmentAttributes.put(key, value);
            return this;
        }

        /**
         * Builds and returns the {@link EvaluationContext}.
         *
         * @return new EvaluationContext instance
         */
        public EvaluationContext build() {
            return new EvaluationContext(this);
        }
    }
}
