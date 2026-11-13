package com.controlvar.sdk.exception;

/**
 * Why a typed getter ({@code getStringValue}, {@code getNumberValue}, {@code getJsonValue})
 * returned the caller's default although the server answered, or was not asked: passed to the
 * {@link com.controlvar.sdk.ErrorListener}, never thrown. A failed request or an unknown flag
 * (404) is passed on as the {@link ExperimentationException} the request failed with instead.
 *
 * <p>It is never an HTTP error: its status code is 0.
 */
public class FlagValueException extends ExperimentationException {

    private static final long serialVersionUID = 1L;

    /** The flag serves a value of another type than the getter reads. */
    public static final String TYPE_MISMATCH = "TYPE_MISMATCH";

    /**
     * The answer has no value (a server from before flag values, or one that cannot serve the
     * flag's stored value), or a value that is not of the type it says; or the getter was given a
     * {@code null} user or an empty flag key, and sent nothing.
     */
    public static final String INVALID_RESPONSE = "INVALID_RESPONSE";

    /** {@link #TYPE_MISMATCH} or {@link #INVALID_RESPONSE}. */
    private final String code;

    /**
     * Constructs the exception.
     *
     * @param message what happened, naming the flag and the getter
     * @param code    {@link #TYPE_MISMATCH} or {@link #INVALID_RESPONSE}
     */
    public FlagValueException(String message, String code) {
        super(message);
        this.code = code;
    }

    /**
     * Returns the reason as a code: {@link #TYPE_MISMATCH} or {@link #INVALID_RESPONSE}, the
     * codes the JavaScript SDK gives the same answers.
     *
     * @return the code
     */
    public String getCode() {
        return code;
    }
}
