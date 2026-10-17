<?php

declare(strict_types=1);

namespace Controlvar;

/**
 * Aggregated result of ExperimentationClient::trackBatch() (POST /api/v1/tracking/batch).
 */
final class BatchResult
{
    /**
     * @param int        $successCount Events the server accepted
     * @param int        $failureCount Events rejected by the server or never delivered
     * @param array|null $errors       Error details (server-provided or local), null when none
     */
    public function __construct(
        public readonly int $successCount,
        public readonly int $failureCount,
        public readonly ?array $errors = null,
    ) {
    }

    /**
     * true when no event failed.
     */
    public function isOk(): bool
    {
        return $this->failureCount === 0;
    }
}
