package com.controlvar.sdk;

import com.controlvar.sdk.model.Exposure;

/**
 * A function the client calls once each time the server enrols a user in an experiment and
 * the answer did not come from the client's cache: the moment the server records the
 * exposure. Use it to send the exposure to your analytics tool or warehouse.
 *
 * <p>Set it with
 * {@link com.controlvar.sdk.config.SdkConfig.Builder#exposureListener(ExposureListener)}, or, with the
 * Spring Boot starter, declare one {@code ExposureListener} bean.
 *
 * <p>{@link ExperimentationClient#getExperimentAssignment} calls it when it stores an answer
 * from {@code POST /api/v1/tracking/assign} that says {@code "assigned": true} for a user and
 * experiment with no unexpired cached answer in that client. It is not called for a cached
 * answer, for the second of two threads that asked for the same new user and experiment at
 * the same moment, for an {@code "assigned": false} answer or one without {@code assigned},
 * for a call that fails, or for a feature flag. It is called again after the cache lifetime,
 * after {@link ExperimentationClient#clearCache()} or
 * {@link ExperimentationClient#invalidateCache(String, String)}, after eviction from a full
 * cache, and in every new client.
 *
 * <p>It runs on the thread that called {@code getExperimentAssignment}, after the answer is
 * cached and before the call returns, with no lock of the client held, so its running time
 * is added to that call and the client cannot time it out. It can run on several threads at
 * once. Hand the exposure to a queue that another thread sends from.
 *
 * <p>If it throws anything, including an {@link Error} such as {@link NoClassDefFoundError},
 * the client catches it and reports nothing: the answer is unchanged. A
 * {@link VirtualMachineError}, such as {@link OutOfMemoryError} or {@link StackOverflowError},
 * is not caught.
 */
@FunctionalInterface
public interface ExposureListener {

    /**
     * Called once for each exposure the server recorded, as described on this interface.
     *
     * @param exposure the experiment key, user id, variant id and variant name; a new object
     *                 for each call
     */
    void onExposure(Exposure exposure);
}
