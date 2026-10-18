package com.controlvar.sdk.exception;

/**
 * Runtime exception thrown by the Controlvar SDK.
 *
 * <p>Wraps API errors (with HTTP status codes) and network/IO errors.
 */
public class ExperimentationException extends RuntimeException {

    private final int statusCode;

    /**
     * Constructs an exception with a message and no HTTP status code (statusCode = 0).
     *
     * @param message human-readable error description
     */
    public ExperimentationException(String message) {
        super(message);
        this.statusCode = 0;
    }

    /**
     * Constructs an exception with a message and an HTTP status code.
     *
     * @param message    human-readable error description
     * @param statusCode HTTP response status code (e.g., 401, 404, 500)
     */
    public ExperimentationException(String message, int statusCode) {
        super(message);
        this.statusCode = statusCode;
    }

    /**
     * Constructs an exception wrapping an underlying cause (e.g., IOException).
     * The statusCode is set to 0.
     *
     * @param message human-readable error description
     * @param cause   the underlying throwable
     */
    public ExperimentationException(String message, Throwable cause) {
        super(message, cause);
        this.statusCode = 0;
    }

    /**
     * Returns the HTTP status code associated with the error, or 0 if not applicable.
     *
     * @return HTTP status code or 0
     */
    public int getStatusCode() {
        return statusCode;
    }

    /**
     * Returns true if this exception was caused by an API error (has a non-zero status code).
     *
     * @return true if statusCode != 0
     */
    public boolean isApiError() {
        return statusCode != 0;
    }
}
