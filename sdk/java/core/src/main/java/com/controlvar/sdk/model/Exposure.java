package com.controlvar.sdk.model;

import java.util.Objects;

/**
 * One exposure the server recorded, as given to an
 * {@link com.controlvar.sdk.ExposureListener}: the experiment, the user and the variant the
 * user was enrolled in.
 *
 * <p>It holds four strings and nothing else: not the user's attributes and not the variant's
 * configuration. It is immutable, and the client builds a new one for each call, so nothing a
 * listener does with it reaches the answer the client keeps.
 */
public final class Exposure {

    private final String experimentKey;
    private final String userId;
    private final String variantId;
    private final String variantName;

    /**
     * Creates an exposure. The client builds these; the constructor is public so that a test
     * of your listener can build one too.
     *
     * @param experimentKey key of the experiment
     * @param userId        identifier of the user the server enrolled
     * @param variantId     id of the variant, or {@code null} when the server sends none
     * @param variantName   name of the variant, e.g. {@code "treatment"}
     */
    public Exposure(String experimentKey, String userId, String variantId, String variantName) {
        this.experimentKey = experimentKey;
        this.userId = userId;
        this.variantId = variantId;
        this.variantName = variantName;
    }

    /**
     * Returns the key of the experiment.
     *
     * @return the experiment key
     */
    public String getExperimentKey() {
        return experimentKey;
    }

    /**
     * Returns the identifier of the user the server enrolled.
     *
     * @return the user id
     */
    public String getUserId() {
        return userId;
    }

    /**
     * Returns the id of the variant the user was enrolled in.
     *
     * @return the variant id, or {@code null} when the server sends none
     */
    public String getVariantId() {
        return variantId;
    }

    /**
     * Returns the name of the variant the user was enrolled in.
     *
     * @return the variant name, e.g. {@code "control"} or {@code "treatment"}
     */
    public String getVariantName() {
        return variantName;
    }

    /**
     * Two exposures are equal when all four values are equal.
     *
     * @param o the object to compare with
     * @return true when {@code o} is an {@code Exposure} with the same four values
     */
    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Exposure)) return false;
        Exposure that = (Exposure) o;
        return Objects.equals(experimentKey, that.experimentKey)
                && Objects.equals(userId, that.userId)
                && Objects.equals(variantId, that.variantId)
                && Objects.equals(variantName, that.variantName);
    }

    /**
     * Returns a hash of the four values.
     *
     * @return the hash code
     */
    @Override
    public int hashCode() {
        return Objects.hash(experimentKey, userId, variantId, variantName);
    }

    /**
     * Returns the four values, for logs.
     *
     * @return a string such as
     *         {@code Exposure{experimentKey='checkout_flow', userId='user-123', variantId='v-2', variantName='treatment'}}
     */
    @Override
    public String toString() {
        return "Exposure{"
                + "experimentKey='" + experimentKey + '\''
                + ", userId='" + userId + '\''
                + ", variantId='" + variantId + '\''
                + ", variantName='" + variantName + '\''
                + '}';
    }
}
