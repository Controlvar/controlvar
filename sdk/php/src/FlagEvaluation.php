<?php

declare(strict_types=1);

namespace Controlvar;

/**
 * Result of a server-side feature flag evaluation
 * (GET /api/v1/feature-flags/evaluate/{key}?user_id=...).
 */
final class FlagEvaluation
{
    /**
     * @param string $key     The flag key you asked for
     * @param bool   $enabled The server's decision for this user
     * @param mixed  $config  null today: the evaluate route returns config: null for every flag
     */
    public function __construct(
        public readonly string $key,
        public readonly bool $enabled,
        public readonly mixed $config = null,
    ) {
    }

    /**
     * The value reported when an evaluation fails (network/HTTP error, unknown flag key).
     */
    public static function disabled(string $key): self
    {
        return new self($key, false, null);
    }

    /**
     * @return array{key: string, enabled: bool, config: mixed}
     */
    public function toArray(): array
    {
        return [
            'key'     => $this->key,
            'enabled' => $this->enabled,
            'config'  => $this->config,
        ];
    }
}
