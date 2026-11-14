package com.controlvar.sdk;

import com.controlvar.sdk.exception.ExperimentationException;

/**
 * A function the client calls each time a typed getter ({@link ExperimentationClient#getStringValue},
 * {@link ExperimentationClient#getNumberValue}, {@link ExperimentationClient#getJsonValue}) returns
 * the caller's default instead of the flag's value, with the reason. Use it to log or count those
 * fallbacks: the getters themselves never throw.
 *
 * <p>Set it with
 * {@link com.controlvar.sdk.config.SdkConfig.Builder#errorListener(ErrorListener)}. It reports the
 * typed getters' fallbacks only: {@link ExperimentationClient#evaluateFeatureFlag} and
 * {@link ExperimentationClient#getExperimentAssignment} throw their failures, and
 * {@link ExperimentationClient#isFeatureEnabled} returns {@code false}, without calling it.
 *
 * <p>It runs on the thread that called the getter, before the getter returns, so its running
 * time is added to that call. It can run on several threads at once.
 *
 * <p>If it throws anything, including an {@link Error} such as {@link NoClassDefFoundError}, the
 * client catches it and the getter still returns the default. A {@link VirtualMachineError}, such
 * as {@link OutOfMemoryError} or {@link StackOverflowError}, is not caught. A thrown
 * {@link InterruptedException} sets the thread's interrupt flag again.
 */
@FunctionalInterface
public interface ErrorListener {

    /**
     * Called once for each call of a typed getter that returned the caller's default.
     *
     * @param error     why: a {@link com.controlvar.sdk.exception.FlagValueException} with its code
     *                  when the server's answer had no value of the getter's type (or the user was
     *                  {@code null} or the flag key empty); otherwise the
     *                  {@link ExperimentationException} the request failed with (its status code is
     *                  the HTTP status, 404 for an unknown flag, or 0 for a network error)
     * @param operation what failed: {@code "value"} for a typed getter, the only operation that
     *                  calls it
     */
    void onError(ExperimentationException error, String operation);
}
