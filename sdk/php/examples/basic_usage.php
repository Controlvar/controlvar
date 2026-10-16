<?php

declare(strict_types=1);

/**
 * Basic usage example for the Controlvar PHP SDK.
 *
 * Demonstrates:
 *  1. Creating a client
 *  2. Evaluating a feature flag (decided by the server)
 *  3. Getting a sticky experiment assignment
 *  4. Tracking events (with a key, and key-less fan-out)
 *  5. The cross-SDK hash utility
 *
 * Run from the sdk/php directory:
 *   composer install
 *   EXPERIMENTLY_API_URL=http://localhost:8000 EXPERIMENTLY_API_KEY=... php examples/basic_usage.php
 *
 * For a runnable end-to-end check against a live backend see contract_smoke.php.
 */

require_once __DIR__ . '/../vendor/autoload.php';

use Controlvar\ExperimentationClient;
use Controlvar\FeatureFlagEvaluator;
use Controlvar\SdkConfig;

// ---------------------------------------------------------------------------
// 1. Configure and create the client
// ---------------------------------------------------------------------------

$config = new SdkConfig(
    baseUrl: getenv('EXPERIMENTLY_API_URL') ?: 'http://localhost:8000', // origin only; the SDK appends /api/v1/...
    apiKey: getenv('EXPERIMENTLY_API_KEY') ?: 'your-api-key-here',       // sent as X-API-Key
    cacheTtl: 300,      // reuse a successful evaluation/assignment for 5 minutes
    timeout: 10,        // HTTP timeout in seconds
    maxCacheSize: 1000, // max cached entries
);

$client = new ExperimentationClient($config);

$userId = 'user-' . random_int(1000, 9999);

// ---------------------------------------------------------------------------
// 2. Evaluate a feature flag — GET /api/v1/feature-flags/evaluate/{key}?user_id=...
// ---------------------------------------------------------------------------

echo "=== Feature Flag Evaluation ===\n";

$flag = $client->evaluateFlag('dark-mode', $userId);

echo "Flag:    {$flag->key}\n";
echo "User:    {$userId}\n";
echo 'Enabled: ' . ($flag->enabled ? 'true' : 'false') . "\n";
echo 'Config:  ' . json_encode($flag->config) . "\n";

if ($client->isFeatureEnabled('dark-mode', $userId)) {   // served from the cache
    echo "Dark mode is ENABLED for this user\n";
} else {
    echo "Dark mode is DISABLED (or the flag is not ACTIVE / the API is unreachable)\n";
}

// ---------------------------------------------------------------------------
// 3. Get an experiment assignment — POST /api/v1/tracking/assign (sticky)
// ---------------------------------------------------------------------------

echo "\n=== Experiment Assignment ===\n";

$assignment = $client->getAssignment('checkout-flow-v2', $userId, ['plan' => 'premium', 'country' => 'US']);

if ($assignment !== null) {
    echo "Experiment: {$assignment->experimentKey}\n";
    echo "Variant:    {$assignment->variantName} (id {$assignment->variantId})\n";
    echo 'Is control: ' . ($assignment->isControl ? 'yes' : 'no') . "\n";
    echo 'Config:     ' . json_encode($assignment->configuration) . "\n";
} else {
    echo "No assignment (experiment not ACTIVE, unknown key, or the API is unreachable)\n";
}

// ---------------------------------------------------------------------------
// 4. Track events — never throws
// ---------------------------------------------------------------------------

echo "\n=== Event Tracking ===\n";

// With a key -> one POST /api/v1/tracking/track
$ok = $client->track('purchase', $userId, ['sku' => 'pro-plan'], 'checkout-flow-v2', null, 99.99);
echo 'Tracked purchase (experiment_key): ' . ($ok ? 'success' : 'failed (non-fatal)') . "\n";

$ok = $client->track('search', $userId, ['q' => 'shoes'], null, 'dark-mode');
echo 'Tracked search (feature_flag_key): ' . ($ok ? 'success' : 'failed (non-fatal)') . "\n";

// Without a key -> one POST /api/v1/tracking/batch with one entry per cached
// assignment and evaluated flag for this user (nothing cached -> nothing sent)
$ok = $client->track('page_view', $userId, ['page' => 'checkout']);
echo 'Tracked page_view (fan-out):       ' . ($ok ? 'success' : 'failed (non-fatal)') . "\n";

// Explicit batch -> BatchResult
$result = $client->trackBatch([
    ['event_name' => 'add_to_cart', 'user_id' => $userId, 'experiment_key' => 'checkout-flow-v2', 'value' => 1.0],
    ['event_name' => 'add_to_cart', 'user_id' => $userId, 'feature_flag_key' => 'dark-mode'],
]);
echo "trackBatch: {$result->successCount} ok, {$result->failureCount} failed\n";

// ---------------------------------------------------------------------------
// 5. Cross-SDK hash utility (no network; not used for bucketing)
// ---------------------------------------------------------------------------

echo "\n=== Hash Utility ===\n";

$known = FeatureFlagEvaluator::hashUser('user-123', 'my-flag');
printf("hashUser('user-123', 'my-flag') = %.16f\n", $known);
echo "Expected (all SDKs):              0.6927449859213084\n";
echo 'Match: ' . (abs($known - 0.6927449859213084) < 1e-10 ? 'YES' : 'NO') . "\n";

echo "\nDone.\n";
