package com.controlvar.sdk.model;

import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;

/**
 * Represents a user of the experimentation platform with optional targeting attributes.
 *
 * <p>Use the builder to construct immutable User instances:
 * <pre>
 *     User user = User.builder("user-123")
 *         .attribute("country", "US")
 *         .attribute("plan", "premium")
 *         .attribute("age", 30)
 *         .build();
 * </pre>
 */
public class User {

    private final String userId;
    private final Map<String, Object> attributes;

    private User(String userId, Map<String, Object> attributes) {
        if (userId == null || userId.isEmpty()) {
            throw new IllegalArgumentException("userId is required and must not be empty");
        }
        this.userId = userId;
        this.attributes = Collections.unmodifiableMap(new HashMap<>(attributes));
    }

    /**
     * Returns the unique identifier for this user.
     *
     * @return user ID (never null or empty)
     */
    public String getUserId() {
        return userId;
    }

    /**
     * Returns an unmodifiable view of the user's targeting attributes.
     *
     * @return immutable map of attribute key-value pairs
     */
    public Map<String, Object> getAttributes() {
        return attributes;
    }

    /**
     * Returns the value of a specific attribute, or null if not present.
     *
     * @param key attribute name
     * @return attribute value or null
     */
    public Object getAttribute(String key) {
        return attributes.get(key);
    }

    /**
     * Creates a new builder for a {@link User} with the given userId.
     *
     * @param userId the unique user identifier (must be non-null and non-empty)
     * @return a new Builder
     */
    public static Builder builder(String userId) {
        return new Builder(userId);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        User user = (User) o;
        return Objects.equals(userId, user.userId) &&
               Objects.equals(attributes, user.attributes);
    }

    @Override
    public int hashCode() {
        return Objects.hash(userId, attributes);
    }

    @Override
    public String toString() {
        return "User{userId='" + userId + "', attributes=" + attributes + "}";
    }

    /**
     * Builder for {@link User}.
     */
    public static class Builder {
        private final String userId;
        private final Map<String, Object> attributes = new HashMap<>();

        /**
         * @param userId unique user identifier
         */
        public Builder(String userId) {
            this.userId = userId;
        }

        /**
         * Adds a targeting attribute.
         *
         * @param key   attribute name (e.g., "country", "plan", "age")
         * @param value attribute value (String, Number, Boolean, etc.)
         * @return this builder
         */
        public Builder attribute(String key, Object value) {
            if (key == null || key.isEmpty()) {
                throw new IllegalArgumentException("Attribute key must not be null or empty");
            }
            attributes.put(key, value);
            return this;
        }

        /**
         * Adds all attributes from the given map.
         *
         * @param attrs map of attribute key-value pairs
         * @return this builder
         */
        public Builder attributes(Map<String, Object> attrs) {
            if (attrs != null) {
                attributes.putAll(attrs);
            }
            return this;
        }

        /**
         * Builds and returns the immutable {@link User}.
         *
         * @return new User instance
         */
        public User build() {
            return new User(userId, attributes);
        }
    }
}
