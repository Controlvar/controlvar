<?php

declare(strict_types=1);

namespace Controlvar\Errors;

/**
 * Thrown when the API returns a 4xx or 5xx HTTP status code.
 */
class ApiException extends ExperimentationException
{
    public function __construct(
        string $message = '',
        public readonly int $statusCode = 0,
        int $code = 0,
        ?\Throwable $previous = null
    ) {
        parent::__construct($message, $code, $previous);
    }

    public function getStatusCode(): int
    {
        return $this->statusCode;
    }
}
