package com.controlvar.sdk.model;

import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/**
 * An analytics event recorded with
 * {@link com.controlvar.sdk.ExperimentationClient#trackEvent(TrackEvent)}.
 *
 * <pre>
 *     TrackEvent event = TrackEvent.builder("user-123", "purchase")
 *         .experimentKey("checkout_flow")
 *         .value(49.99)
 *         .property("currency", "USD")
 *         .build();
 * </pre>
 *
 * <p>When neither {@code experimentKey} nor {@code featureFlagKey} is set, the
 * client fans the event out to every experiment the user was assigned to and
 * every flag evaluated for the user (from its cache).
 */
public class TrackEvent {

    private final String userId;
    private final String eventName;
    private final String eventType;
    private final String experimentKey;
    private final String featureFlagKey;
    private final Double value;
    private final Map<String, Object> properties;
    private final Instant timestamp;

    private TrackEvent(Builder builder) {
        this.userId = builder.userId;
        this.eventName = builder.eventName;
        this.eventType = builder.eventType != null ? builder.eventType : builder.eventName;
        this.experimentKey = builder.experimentKey;
        this.featureFlagKey = builder.featureFlagKey;
        this.value = builder.value;
        this.properties = Collections.unmodifiableMap(new HashMap<>(builder.properties));
        this.timestamp = builder.timestamp;
    }

    public String getUserId() { return userId; }

    /** Returns the event name; experiment metrics are matched by event name. */
    public String getEventName() { return eventName; }

    /** Returns the event type sent as {@code event_type}; defaults to the event name. */
    public String getEventType() { return eventType; }

    public String getExperimentKey() { return experimentKey; }

    public String getFeatureFlagKey() { return featureFlagKey; }

    /** Returns the optional numeric value (revenue, duration, ...). */
    public Double getValue() { return value; }

    /** Returns the event properties, sent as {@code metadata}. */
    public Map<String, Object> getProperties() { return properties; }

    /** Returns the explicit timestamp, or {@code null} to let the server stamp the event. */
    public Instant getTimestamp() { return timestamp; }

    /** Returns true when the event is attributed to an experiment or a feature flag. */
    public boolean hasKey() {
        return (experimentKey != null && !experimentKey.isEmpty())
            || (featureFlagKey != null && !featureFlagKey.isEmpty());
    }

    /**
     * Returns the wire form of this event: the body of {@code POST /api/v1/tracking/track}
     * and of each {@code POST /api/v1/tracking/batch} entry.
     */
    public Map<String, Object> toBody() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("event_type", eventType);
        body.put("event_name", eventName);
        body.put("user_id", userId);
        if (experimentKey != null && !experimentKey.isEmpty()) {
            body.put("experiment_key", experimentKey);
        }
        if (featureFlagKey != null && !featureFlagKey.isEmpty()) {
            body.put("feature_flag_key", featureFlagKey);
        }
        if (value != null) {
            body.put("value", value);
        }
        if (!properties.isEmpty()) {
            body.put("metadata", properties);
        }
        if (timestamp != null) {
            body.put("timestamp", DateTimeFormatter.ISO_INSTANT.format(timestamp));
        }
        return body;
    }

    /**
     * Creates a new builder.
     *
     * @param userId    the user who performed the action (required)
     * @param eventName the event name, e.g. "purchase" (required)
     * @return a new Builder
     * @throws IllegalArgumentException if either argument is null or empty
     */
    public static Builder builder(String userId, String eventName) {
        return new Builder(userId, eventName);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        TrackEvent that = (TrackEvent) o;
        return Objects.equals(userId, that.userId) &&
               Objects.equals(eventName, that.eventName) &&
               Objects.equals(eventType, that.eventType) &&
               Objects.equals(experimentKey, that.experimentKey) &&
               Objects.equals(featureFlagKey, that.featureFlagKey) &&
               Objects.equals(value, that.value) &&
               Objects.equals(properties, that.properties) &&
               Objects.equals(timestamp, that.timestamp);
    }

    @Override
    public int hashCode() {
        return Objects.hash(userId, eventName, eventType, experimentKey, featureFlagKey, value, properties, timestamp);
    }

    @Override
    public String toString() {
        return "TrackEvent{userId='" + userId + "', eventName='" + eventName + "', eventType='" + eventType +
               "', experimentKey='" + experimentKey + "', featureFlagKey='" + featureFlagKey +
               "', value=" + value + ", properties=" + properties + ", timestamp=" + timestamp + "}";
    }

    /** Builder for {@link TrackEvent}. */
    public static class Builder {
        private final String userId;
        private final String eventName;
        private String eventType;
        private String experimentKey;
        private String featureFlagKey;
        private Double value;
        private final Map<String, Object> properties = new HashMap<>();
        private Instant timestamp;

        public Builder(String userId, String eventName) {
            if (userId == null || userId.isEmpty()) {
                throw new IllegalArgumentException("userId is required and must not be empty");
            }
            if (eventName == null || eventName.isEmpty()) {
                throw new IllegalArgumentException("eventName is required and must not be empty");
            }
            this.userId = userId;
            this.eventName = eventName;
        }

        /** Sets {@code event_type} (defaults to the event name). */
        public Builder eventType(String eventType) {
            this.eventType = eventType;
            return this;
        }

        /** Attributes the event to one experiment. */
        public Builder experimentKey(String experimentKey) {
            this.experimentKey = experimentKey;
            return this;
        }

        /** Attributes the event to one feature flag. */
        public Builder featureFlagKey(String featureFlagKey) {
            this.featureFlagKey = featureFlagKey;
            return this;
        }

        /** Sets the numeric value (revenue, duration, ...). */
        public Builder value(Double value) {
            this.value = value;
            return this;
        }

        /** Adds one property (sent inside {@code metadata}). */
        public Builder property(String key, Object value) {
            if (key == null || key.isEmpty()) {
                throw new IllegalArgumentException("Property key must not be null or empty");
            }
            properties.put(key, value);
            return this;
        }

        /** Adds all properties from the map (may be null). */
        public Builder properties(Map<String, Object> props) {
            if (props != null) {
                properties.putAll(props);
            }
            return this;
        }

        /** Sets an explicit timestamp, sent as ISO-8601. */
        public Builder timestamp(Instant timestamp) {
            this.timestamp = timestamp;
            return this;
        }

        public TrackEvent build() {
            return new TrackEvent(this);
        }
    }
}
