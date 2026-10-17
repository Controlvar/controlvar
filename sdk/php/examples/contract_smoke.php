<?php

declare(strict_types=1);

/**
 * Contract smoke for the PHP SDK — exercises the public API against a live
 * backend and prints exactly one JSON line on stdout.
 *
 * Run from the repository root (no composer install needed):
 *   CONTROLVAR_API_KEY=<key> php sdk/php/examples/contract_smoke.php
 * or through the live runner, which seeds the fixtures and supplies the key:
 *   python tests/sdk-contract/live/run_live_contract.py --sdk php --strict
 *
 * Environment:
 *   CONTROLVAR_API_URL     backend origin (default http://localhost:8000)
 *   CONTROLVAR_API_KEY     API key (required)
 *   CONTRACT_EXPERIMENT_KEY  default sdk_contract_ab
 *   CONTRACT_FLAG_KEY        default sdk_contract_flag
 *   CONTRACT_USER_ID         default smoke-<uuid>
 *
 * On success: one JSON line on stdout, exit 0. On failure: one line on stderr,
 * exit 1. Nothing else is ever written to stdout (PHP notices go to stderr).
 */

use Controlvar\ExperimentationClient;
use Controlvar\SdkConfig;

// Keep stdout clean: any PHP warning/notice/deprecation goes to stderr.
error_reporting(E_ALL);
ini_set('display_errors', 'stderr');

$autoload = __DIR__ . '/../vendor/autoload.php';
if (is_file($autoload)) {
    require_once $autoload;
} else {
    // Dependency order matters without an autoloader: base classes first.
    foreach (
        [
            'Errors/ExperimentationException.php',
            'Errors/ApiException.php',
            'Errors/AuthException.php',
            'Errors/NetworkException.php',
            'SdkConfig.php',
            'HttpClient.php',
            'Cache.php',
            'FeatureFlagEvaluator.php',
            'FlagEvaluation.php',
            'Assignment.php',
            'BatchResult.php',
            'ExperimentationClient.php',
        ] as $file
    ) {
        require_once __DIR__ . '/../src/' . $file;
    }
}

function smokeFail(string $message): never
{
    fwrite(STDERR, "contract_smoke(php): {$message}\n");
    exit(1);
}

function envOr(string $name, string $default): string
{
    $value = getenv($name);
    return ($value === false || $value === '') ? $default : $value;
}

/** Random RFC 4122 v4 UUID prefixed with "smoke-". */
function randomUserId(): string
{
    $bytes = random_bytes(16);
    $bytes[6] = chr((ord($bytes[6]) & 0x0f) | 0x40);
    $bytes[8] = chr((ord($bytes[8]) & 0x3f) | 0x80);
    return 'smoke-' . vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($bytes), 4));
}

/**
 * Runs the four contract steps through the SDK's public API and returns the report.
 *
 * @return array<string, mixed>
 */
function runSmoke(string $apiUrl, string $apiKey, string $experimentKey, string $flagKey, string $userId): array
{
    $client     = new ExperimentationClient(new SdkConfig(baseUrl: $apiUrl, apiKey: $apiKey, timeout: 10));
    $attributes = ['source' => 'contract_smoke', 'sdk' => 'php'];

    // 1. Sticky assignment: assign twice, dropping the local cache in between so
    //    the second answer really comes from the server.
    $first = $client->getAssignment($experimentKey, $userId, $attributes);
    if ($first === null) {
        smokeFail("assignment failed for {$experimentKey} (is the experiment ACTIVE and the API key valid?)");
    }
    $client->clearCache();
    $second = $client->getAssignment($experimentKey, $userId, $attributes);
    if ($second === null) {
        smokeFail("second assignment failed for {$experimentKey}");
    }
    if (!in_array($first->variantName, ['control', 'treatment'], true)) {
        smokeFail('unexpected variant_name ' . var_export($first->variantName, true));
    }
    $sticky = $first->variantName === $second->variantName && $first->variantId === $second->variantId;
    if (!$sticky) {
        smokeFail("assignment not sticky: {$first->variantName} then {$second->variantName}");
    }

    // 2. Flag evaluation (the seeded flag is 100% on).
    $flag = $client->evaluateFlag($flagKey, $userId);
    if ($flag->enabled !== true) {
        smokeFail("flag {$flagKey} evaluated as " . var_export($flag->enabled, true) . ', expected true');
    }

    // 3. Track with an experiment key -> one POST /api/v1/tracking/track.
    if (!$client->track('purchase', $userId, ['source' => 'contract_smoke'], $experimentKey, null, 12.5)) {
        smokeFail('track(purchase) with experiment_key failed');
    }

    // 4. Track without a key -> POST /api/v1/tracking/batch fanned out to the
    //    cached assignment + evaluated flag.
    if (!$client->track('page_view', $userId, ['page' => '/smoke'])) {
        smokeFail('track(page_view) fan-out failed');
    }

    // 4b. Explicit two-event batch.
    $batch = $client->trackBatch([
        ['event_name' => 'page_view', 'user_id' => $userId, 'experiment_key' => $experimentKey, 'properties' => ['page' => '/batch']],
        ['event_name' => 'page_view', 'user_id' => $userId, 'feature_flag_key' => $flagKey, 'properties' => ['page' => '/batch']],
    ]);
    if (!$batch->isOk()) {
        smokeFail('trackBatch failed: ' . json_encode($batch->errors));
    }

    return [
        'sdk'    => 'php',
        'assign' => ['variant_name' => $first->variantName, 'is_control' => $first->isControl, 'sticky' => $sticky],
        'flag'   => ['enabled' => $flag->enabled],
        'track'  => ['ok' => true],
        'fanout' => ['ok' => true],
    ];
}

$apiKey = envOr('CONTROLVAR_API_KEY', envOr('EXPERIMENTLY_API_KEY', ''));
if (trim($apiKey) === '') {
    smokeFail('CONTROLVAR_API_KEY is required');
}

try {
    $report = runSmoke(
        envOr('CONTROLVAR_API_URL', envOr('EXPERIMENTLY_API_URL', 'http://localhost:8000')),
        $apiKey,
        envOr('CONTRACT_EXPERIMENT_KEY', 'sdk_contract_ab'),
        envOr('CONTRACT_FLAG_KEY', 'sdk_contract_flag'),
        envOr('CONTRACT_USER_ID', randomUserId()),
    );
    echo json_encode($report, JSON_THROW_ON_ERROR), "\n";
} catch (Throwable $e) {
    smokeFail($e::class . ': ' . $e->getMessage());
}

exit(0);
