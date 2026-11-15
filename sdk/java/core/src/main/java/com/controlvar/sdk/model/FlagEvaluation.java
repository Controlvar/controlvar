package com.controlvar.sdk.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * The server's evaluation of one feature flag for one user, as returned by
 * {@code GET /api/v1/feature-flags/evaluate/{flagKey}?user_id=...&context=...} (the user's
 * attributes are sent as {@code context}) and by
 * {@link com.controlvar.sdk.ExperimentationClient#evaluateFeatureFlag}.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class FlagEvaluation {

    @JsonProperty("key")
    private String key;

    @JsonProperty("enabled")
    private boolean enabled;

    @JsonProperty("config")
    private Object config;

    /** The answer's {@code value} as decoded, never handed out: {@link #getValue()} copies it. */
    @JsonProperty("value")
    private Object value;

    /** The answer's {@code value_type}; only a string is a type ({@link #getValueType()}). */
    @JsonProperty("value_type")
    private Object valueType;

    /** No-arg constructor required for Jackson deserialization. */
    public FlagEvaluation() {}

    /**
     * Constructor for tests and manual construction. It sets no value and no value type
     * ({@link #getValue()} and {@link #getValueType()} return {@code null}), so the evaluation it
     * builds does not equal a server's answer that carries them.
     *
     * @param key     the evaluated flag key
     * @param enabled the server's decision for this user
     * @param config  {@code null} today: the evaluate route returns {@code config: null} for every flag
     */
    public FlagEvaluation(String key, boolean enabled, Object config) {
        this.key = key;
        this.enabled = enabled;
        this.config = config;
    }

    /** Returns the evaluated flag key. */
    public String getKey() { return key; }
    public void setKey(String key) { this.key = key; }

    /** Returns whether the flag is on for this user. */
    public boolean isEnabled() { return enabled; }
    public void setEnabled(boolean enabled) { this.enabled = enabled; }

    /**
     * Returns {@code null} today: the evaluate route returns {@code config: null}
     * for every flag. A JSON value the server might send decodes to a
     * {@code Map} (object), a {@code List} (array) or a {@code String},
     * {@code Number} or {@code Boolean} (scalar).
     */
    public Object getConfig() { return config; }
    public void setConfig(Object config) { this.config = config; }

    /**
     * Returns the config as a map when the server returned a JSON object,
     * {@code null} otherwise.
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> getConfigMap() {
        return (config instanceof Map) ? (Map<String, Object>) config : null;
    }

    /**
     * Returns the value the flag serves this user, as a new copy on every call: the on value
     * when the flag is on for the user, the off value otherwise. A {@code String}, a
     * {@code Number} (an {@code Integer}, {@code Long}, {@code BigInteger} or {@code Double}, as
     * the JSON number was written), a {@code Boolean}, a {@code Map} (a JSON object) or a
     * {@code List} (a JSON array). Changing a returned map or list changes nothing this
     * evaluation, or the client's cache, holds.
     *
     * <p>{@code null} when the server sent none: a server from before flag values, or one that
     * cannot serve the flag's stored value. The typed getters
     * ({@link com.controlvar.sdk.ExperimentationClient#getStringValue} and the others) check the
     * value's type for you.
     *
     * @return a copy of the value, or {@code null}
     */
    public Object getValue() {
        return copy(value);
    }

    /**
     * Returns the type of value the flag serves, as the server names it: {@code "string"},
     * {@code "number"}, {@code "json"} or {@code "boolean"}.
     *
     * @return the type, or {@code null} when the server sent none (or sent one that is not a string)
     */
    public String getValueType() {
        return valueType instanceof String ? (String) valueType : null;
    }

    /** A copy that shares no map or list with {@code value}; anything else is immutable. */
    private static Object copy(Object value) {
        if (value instanceof Map) {
            Map<Object, Object> copied = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : ((Map<?, ?>) value).entrySet()) {
                copied.put(entry.getKey(), copy(entry.getValue()));
            }
            return copied;
        }
        if (value instanceof List) {
            List<Object> copied = new ArrayList<>(((List<?>) value).size());
            for (Object item : (List<?>) value) {
                copied.add(copy(item));
            }
            return copied;
        }
        return value;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        FlagEvaluation that = (FlagEvaluation) o;
        return enabled == that.enabled &&
               Objects.equals(key, that.key) &&
               Objects.equals(config, that.config) &&
               Objects.equals(value, that.value) &&
               Objects.equals(valueType, that.valueType);
    }

    @Override
    public int hashCode() {
        return Objects.hash(key, enabled, config, value, valueType);
    }

    @Override
    public String toString() {
        return "FlagEvaluation{key='" + key + "', enabled=" + enabled + ", config=" + config
                + ", value=" + value + ", valueType=" + valueType + "}";
    }
}
