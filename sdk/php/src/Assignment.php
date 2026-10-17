<?php

declare(strict_types=1);

namespace Controlvar;

/**
 * Result of a sticky experiment assignment (POST /api/v1/tracking/assign).
 */
final class Assignment
{
    /**
     * @param string      $experimentKey The experiment key you asked for
     * @param string|null $variantId     UUID of the assigned variant
     * @param string      $variantName   Assigned variant name (e.g. "control", "treatment")
     * @param bool        $isControl     true for the control variant
     * @param array|null  $configuration The variant's configuration JSON
     */
    public function __construct(
        public readonly string $experimentKey,
        public readonly ?string $variantId,
        public readonly string $variantName,
        public readonly bool $isControl,
        public readonly ?array $configuration = null,
    ) {
    }

    /**
     * @return array{experiment_key: string, variant_id: string|null, variant_name: string, is_control: bool, configuration: array|null}
     */
    public function toArray(): array
    {
        return [
            'experiment_key' => $this->experimentKey,
            'variant_id'     => $this->variantId,
            'variant_name'   => $this->variantName,
            'is_control'     => $this->isControl,
            'configuration'  => $this->configuration,
        ];
    }
}
