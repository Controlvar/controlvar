package com.controlvar.sdk;

import com.controlvar.sdk.exception.ExperimentationException;

/**
 * The one path between an {@link ErrorListener} and the client, as {@link ExposureHook} is for an
 * {@link ExposureListener}: nothing the listener does reaches the client's own code, so it can
 * never change or fail an answer.
 *
 * <p>Not public API.
 */
final class ErrorHook {

    private ErrorHook() {}

    /**
     * Calls {@code listener} with {@code error} and {@code operation} on this thread and catches
     * whatever it throws, an {@link Error} such as {@link NoClassDefFoundError} included. A
     * {@link VirtualMachineError} (an {@link OutOfMemoryError}, a {@link StackOverflowError}) is
     * not caught. An {@link InterruptedException} thrown without being declared is caught, and the
     * thread's interrupt flag is set again; a flag the listener set itself is left set. What it
     * threw is dropped: there is no channel left to report it to.
     *
     * @param listener  the listener, or {@code null} for none
     * @param error     the reason to give it
     * @param operation the operation that failed
     */
    static void call(ErrorListener listener, ExperimentationException error, String operation) {
        if (listener == null) return;
        try {
            listener.onError(error, operation);
        } catch (VirtualMachineError fatal) {
            throw fatal;
        } catch (Throwable thrown) {
            if (thrown instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
        }
    }
}
