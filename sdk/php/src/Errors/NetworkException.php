<?php

declare(strict_types=1);

namespace Controlvar\Errors;

/**
 * Thrown when a network-level error occurs (cURL failure, timeout, etc.).
 */
class NetworkException extends ExperimentationException
{
    public function __construct(string $message = '', int $code = 0, ?\Throwable $previous = null)
    {
        parent::__construct($message, $code, $previous);
    }
}
