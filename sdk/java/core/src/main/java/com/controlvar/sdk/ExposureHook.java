package com.controlvar.sdk;

import com.controlvar.sdk.exception.ExperimentationException;
import com.controlvar.sdk.model.Exposure;

/**
 * The one path between an {@link ExposureListener} and the client: nothing the listener does
 * reaches the client's own code, so it can never change or fail an answer.
 *
 * <p>Not public API.
 */
final class ExposureHook {

    /** How the message of every {@link HookError} starts. */
    static final String MESSAGE_PREFIX = "onExposure failed: ";

    private ExposureHook() {}

    /**
     * Calls {@code listener} with {@code exposure} on this thread and catches whatever it
     * throws, an {@link Error} such as {@link NoClassDefFoundError} included. A
     * {@link VirtualMachineError} (an {@link OutOfMemoryError}, a {@link StackOverflowError})
     * is not caught. An {@link InterruptedException} thrown without being declared is caught,
     * and the thread's interrupt flag is set again.
     *
     * @param listener the listener, or {@code null} for none
     * @param exposure what to give it
     * @return {@code null} when there is no listener or it returned normally; otherwise the
     *         hook error for what it threw
     */
    static HookError call(ExposureListener listener, Exposure exposure) {
        if (listener == null) return null;
        try {
            listener.onExposure(exposure);
            return null;
        } catch (VirtualMachineError fatal) {
            throw fatal;
        } catch (Throwable thrown) {
            if (thrown instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            return error(thrown);
        }
    }

    /**
     * The hook error for what a listener threw: the message is {@link #MESSAGE_PREFIX} and
     * the thrown value's message (its class name when it has none), and the cause is the
     * thrown value itself.
     *
     * @param thrown what the listener threw
     * @return the hook error
     */
    static HookError error(Throwable thrown) {
        return new HookError(MESSAGE_PREFIX + describe(thrown), thrown);
    }

    private static String describe(Throwable thrown) {
        try {
            String message = thrown.getMessage();
            if (message != null && !message.isEmpty()) return message;
        } catch (VirtualMachineError fatal) {
            throw fatal;
        } catch (Throwable unreadable) {
            // a getMessage() that throws: the class name below still says what it was
        }
        return thrown.getClass().getName();
    }

    /**
     * The Java SDK's kind for a failure in an {@link ExposureListener}, as {@code HOOK_ERROR}
     * is the JavaScript SDK's. It is never a network error: its status code is 0, its message
     * starts with {@link #MESSAGE_PREFIX} and its cause is what the listener threw.
     */
    static final class HookError extends ExperimentationException {
        private static final long serialVersionUID = 1L;

        HookError(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
