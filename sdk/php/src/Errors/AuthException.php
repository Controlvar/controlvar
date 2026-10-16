<?php

declare(strict_types=1);

namespace Controlvar\Errors;

/**
 * Thrown when the API returns a 401 Unauthorized HTTP status code.
 * Typically indicates an invalid or missing API key.
 */
class AuthException extends ApiException
{
    public function __construct(string $message = 'Unauthorized: invalid or missing API key', ?\Throwable $previous = null)
    {
        parent::__construct($message, 401, 0, $previous);
    }
}
