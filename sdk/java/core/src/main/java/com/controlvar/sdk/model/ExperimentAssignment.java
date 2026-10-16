package com.controlvar.sdk.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Map;
import java.util.Objects;

/**
 * The variant the server assigned a user to, as returned by
 * {@code POST /api/v1/tracking/assign} and by
 * {@link com.controlvar.sdk.ExperimentationClient#getExperimentAssignment}.
 *
 * <p>Assignments are sticky server-side: the same user always receives the same
 * variant for a given experiment.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class ExperimentAssignment {

    @JsonProperty("experiment_key")
    private String experimentKey;

    @JsonProperty("user_id")
    private String userId;

    @JsonProperty("variant_id")
    private String variantId;

    @JsonProperty("variant_name")
    private String variantName;

    @JsonProperty("is_control")
    private boolean control;

    @JsonProperty("configuration")
    private Map<String, Object> configuration;

    /** No-arg constructor required for Jackson deserialization. */
    public ExperimentAssignment() {}

    /**
     * Full constructor for tests and manual construction.
     *
     * @param experimentKey key of the experiment
     * @param userId        identifier of the user who was assigned
     * @param variantId     UUID of the assigned variant
     * @param variantName   name of the assigned variant (e.g. "control", "treatment")
     * @param control       true for the control variant
     * @param configuration the variant's configuration JSON, or {@code null}
     */
    public ExperimentAssignment(String experimentKey, String userId, String variantId,
                                String variantName, boolean control, Map<String, Object> configuration) {
        this.experimentKey = experimentKey;
        this.userId = userId;
        this.variantId = variantId;
        this.variantName = variantName;
        this.control = control;
        this.configuration = configuration;
    }

    public String getExperimentKey() { return experimentKey; }
    public void setExperimentKey(String experimentKey) { this.experimentKey = experimentKey; }

    public String getUserId() { return userId; }
    public void setUserId(String userId) { this.userId = userId; }

    /** Returns the assigned variant's UUID. */
    public String getVariantId() { return variantId; }
    public void setVariantId(String variantId) { this.variantId = variantId; }

    /** Returns the assigned variant's name, e.g. {@code "control"} or {@code "treatment"}. */
    public String getVariantName() { return variantName; }
    public void setVariantName(String variantName) { this.variantName = variantName; }

    /**
     * Alias for {@link #getVariantName()}.
     *
     * @deprecated the server identifies variants by name; use {@link #getVariantName()}.
     */
    @Deprecated
    public String getVariantKey() { return variantName; }

    /** Returns true when the user is in the control variant. */
    public boolean isControl() { return control; }
    public void setControl(boolean control) { this.control = control; }

    /** Returns the variant's configuration JSON from the experiment definition, or {@code null}. */
    public Map<String, Object> getConfiguration() { return configuration; }
    public void setConfiguration(Map<String, Object> configuration) { this.configuration = configuration; }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        ExperimentAssignment that = (ExperimentAssignment) o;
        return control == that.control &&
               Objects.equals(experimentKey, that.experimentKey) &&
               Objects.equals(userId, that.userId) &&
               Objects.equals(variantId, that.variantId) &&
               Objects.equals(variantName, that.variantName) &&
               Objects.equals(configuration, that.configuration);
    }

    @Override
    public int hashCode() {
        return Objects.hash(experimentKey, userId, variantId, variantName, control, configuration);
    }

    @Override
    public String toString() {
        return "ExperimentAssignment{" +
               "experimentKey='" + experimentKey + '\'' +
               ", userId='" + userId + '\'' +
               ", variantId='" + variantId + '\'' +
               ", variantName='" + variantName + '\'' +
               ", control=" + control +
               ", configuration=" + configuration +
               '}';
    }
}
