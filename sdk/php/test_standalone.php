<?php

declare(strict_types=1);

/**
 * Standalone test runner — no composer/autoloader required.
 *
 * Verifies the cross-SDK hash vector, the result types, the cache and the
 * client's request shapes (through a fake HttpClient) without any external
 * dependency or network. Useful on systems where composer is not set up.
 *
 * Run: php sdk/php/test_standalone.php
 */

foreach (
    [
        'src/Errors/ExperimentationException.php',
        'src/Errors/ApiException.php',
        'src/Errors/AuthException.php',
        'src/Errors/NetworkException.php',
        'src/SdkConfig.php',
        'src/HttpClient.php',
        'src/Cache.php',
        'src/FeatureFlagEvaluator.php',
        'src/FlagEvaluation.php',
        'src/Assignment.php',
        'src/BatchResult.php',
        'src/ExperimentationClient.php',
        'tests/FakeHttpClient.php',
    ] as $file
) {
    require_once __DIR__ . '/' . $file;
}

use Controlvar\Assignment;
use Controlvar\BatchResult;
use Controlvar\Cache;
use Controlvar\Errors\ApiException;
use Controlvar\Errors\NetworkException;
use Controlvar\ExperimentationClient;
use Controlvar\FeatureFlagEvaluator;
use Controlvar\FlagEvaluation;
use Controlvar\SdkConfig;
use Controlvar\Tests\FakeHttpClient;

$failures = [];
$passed   = 0;

function ok(string $name): void
{
    global $passed;
    $passed++;
    echo "  PASS  $name\n";
}

function fail(string $name, string $reason): void
{
    global $failures;
    $failures[] = "  FAIL  $name: $reason";
    echo "  FAIL  $name: $reason\n";
}

function check(string $name, bool $condition, string $reason = ''): void
{
    if ($condition) {
        ok($name);
    } else {
        fail($name, $reason !== '' ? $reason : 'assertion failed');
    }
}

function makeClient(FakeHttpClient $http, int $cacheTtl = 300): ExperimentationClient
{
    return new ExperimentationClient(
        new SdkConfig(baseUrl: 'https://api.example.com', apiKey: 'test-key', cacheTtl: $cacheTtl, timeout: 5),
        $http
    );
}

// =============================================================================
// Hash parity — cross-SDK compatibility vector (utility only)
// =============================================================================

echo "\n=== Hash Compatibility ===\n";

$result   = FeatureFlagEvaluator::hashUser('user-123', 'my-flag');
$expected = 0.6927449859213084;
check('hashUser("user-123", "my-flag") === 0.6927449859213084', abs($result - $expected) < 1e-10, "got $result");

$outOfBounds = false;
for ($i = 0; $i < 100; $i++) {
    $h = FeatureFlagEvaluator::hashUser("user-$i", "flag-$i");
    if ($h < 0.0 || $h >= 1.0) {
        $outOfBounds = true;
        break;
    }
}
check('all 100 hash values are in [0.0, 1.0)', !$outOfBounds);

$h1 = FeatureFlagEvaluator::hashUser('det-user', 'det-flag');
$h2 = FeatureFlagEvaluator::hashUser('det-user', 'det-flag');
check('hashUser is deterministic', $h1 === $h2);

$hEmpty = FeatureFlagEvaluator::hashUser('', '');
check('hashUser("", "") returns a float in [0, 1)', $hEmpty >= 0.0 && $hEmpty < 1.0);

$hUnicode = FeatureFlagEvaluator::hashUser('用户-123', '功能标志');
check('hashUser(unicode) returns a float in [0, 1)', $hUnicode >= 0.0 && $hUnicode < 1.0);

check('local evaluation was removed (the server decides)', !method_exists(FeatureFlagEvaluator::class, 'evaluate'));

// =============================================================================
// Result types
// =============================================================================

echo "\n=== Result Types ===\n";

$flag = new FlagEvaluation('dark-mode', true, ['theme' => 'dark']);
check('FlagEvaluation exposes key/enabled/config', $flag->key === 'dark-mode' && $flag->enabled && $flag->config === ['theme' => 'dark']);
check('FlagEvaluation::disabled() is off with a null config', !FlagEvaluation::disabled('x')->enabled && FlagEvaluation::disabled('x')->config === null);
check('FlagEvaluation::toArray()', $flag->toArray() === ['key' => 'dark-mode', 'enabled' => true, 'config' => ['theme' => 'dark']]);

$assignment = new Assignment('exp', 'v1', 'control', true, null);
check('Assignment exposes experimentKey/variantId/variantName/isControl/configuration',
    $assignment->experimentKey === 'exp' && $assignment->variantId === 'v1'
    && $assignment->variantName === 'control' && $assignment->isControl && $assignment->configuration === null);
check('Assignment::toArray() uses the API field names', $assignment->toArray()['variant_name'] === 'control');

$batch = new BatchResult(2, 0, null);
check('BatchResult::isOk() without failures', $batch->isOk());
check('BatchResult::isOk() with failures is false', !(new BatchResult(1, 1, [['error' => 'x']]))->isOk());

// =============================================================================
// Cache
// =============================================================================

echo "\n=== Cache ===\n";

$cache = new Cache(300, 3);
check('get() returns null for a missing key', $cache->get('x') === null);
$cache->set('x', 42);
check('get() returns the stored value', $cache->get('x') === 42);
$cache->clear();
check('clear() removes all entries', $cache->get('x') === null && $cache->count() === 0);

$cache->set('k0', 0);
$cache->set('k1', 1);
$cache->set('k2', 2);
$cache->set('k3', 3);
check('evicts the oldest entry at capacity', $cache->get('k0') === null && $cache->get('k3') === 3);

$cache->clear();
$cache->set('flag:2:u1:a', 'A');
$cache->set('flag:2:u2:b', 'B');
$cache->set('flag:2:u1:c', 'C');
check('valuesWithPrefix() lists one user\'s entries in insertion order', $cache->valuesWithPrefix('flag:2:u1:') === ['A', 'C']);

// =============================================================================
// Client — request shapes through the fake HttpClient
// =============================================================================

echo "\n=== Client (fake HTTP) ===\n";

$assignPath   = '/api/v1/tracking/assign';
$evaluatePath = '/api/v1/feature-flags/evaluate/dark-mode?user_id=user-123';
$trackPath    = '/api/v1/tracking/track';
$batchPath    = '/api/v1/tracking/batch';

$http = new FakeHttpClient();
$http->on('POST', $assignPath, [
    'experiment_key' => 'checkout-flow', 'user_id' => 'user-123', 'variant_id' => 'var-1',
    'variant_name' => 'treatment', 'is_control' => false, 'configuration' => ['color' => 'green'],
]);
$http->on('GET', $evaluatePath, ['key' => 'dark-mode', 'enabled' => true, 'config' => null]);
$http->on('POST', $trackPath, ['id' => 'evt-1']);
$http->on('POST', $batchPath, ['success_count' => 2, 'failure_count' => 0, 'errors' => null]);
$client = makeClient($http);

$a = $client->getAssignment('checkout-flow', 'user-123', ['country' => 'US']);
check('getAssignment() POSTs /api/v1/tracking/assign with experiment_key/user_id/context',
    $http->requests[0]['method'] === 'POST' && $http->requests[0]['path'] === $assignPath
    && $http->requests[0]['body'] === ['experiment_key' => 'checkout-flow', 'user_id' => 'user-123', 'context' => ['country' => 'US']]);
check('getAssignment() maps the response', $a instanceof Assignment && $a->variantName === 'treatment' && !$a->isControl && $a->configuration === ['color' => 'green']);
$client->getAssignment('checkout-flow', 'user-123');
check('getAssignment() is sticky (served from cache)', count($http->requestsTo($assignPath)) === 1);

$f = $client->evaluateFlag('dark-mode', 'user-123');
check('evaluateFlag() GETs /api/v1/feature-flags/evaluate/{key}?user_id=…', $http->requests[1]['method'] === 'GET' && $http->requests[1]['path'] === $evaluatePath);
check('evaluateFlag() maps the response', $f->key === 'dark-mode' && $f->enabled && $f->config === null);
check('isFeatureEnabled() is true (cache hit, no new request)', $client->isFeatureEnabled('dark-mode', 'user-123') && count($http->requestsTo($evaluatePath)) === 1);

$ok = $client->track('purchase', 'user-123', ['sku' => 'pro'], 'checkout-flow', null, 12.5);
$trackBody = $http->requestsTo($trackPath)[0]['body'] ?? [];
check('track() with a key POSTs one /api/v1/tracking/track', $ok && count($http->requestsTo($trackPath)) === 1);
check('track() body has event_type/event_name/user_id/experiment_key/value/metadata/timestamp',
    ($trackBody['event_type'] ?? null) === 'purchase' && ($trackBody['event_name'] ?? null) === 'purchase'
    && ($trackBody['user_id'] ?? null) === 'user-123' && ($trackBody['experiment_key'] ?? null) === 'checkout-flow'
    && ($trackBody['value'] ?? null) === 12.5 && ($trackBody['metadata'] ?? null) === ['sku' => 'pro']
    && is_string($trackBody['timestamp'] ?? null) && !array_key_exists('feature_flag_key', $trackBody));

$ok = $client->track('page_view', 'user-123', ['page' => '/']);
$batchEvents = $http->requestsTo($batchPath)[0]['body']['events'] ?? [];
check('track() without a key fans out one /api/v1/tracking/batch', $ok && count($http->requestsTo($batchPath)) === 1);
check('fan-out has one entry per cached assignment + evaluated flag',
    count($batchEvents) === 2 && ($batchEvents[0]['experiment_key'] ?? null) === 'checkout-flow'
    && ($batchEvents[1]['feature_flag_key'] ?? null) === 'dark-mode'
    && ($batchEvents[0]['metadata'] ?? null) === ['page' => '/']);

$before = count($http->requests);
check('track() without a key and nothing cached sends nothing', $client->track('page_view', 'user-999') && count($http->requests) === $before);

$r = $client->trackBatch([
    ['event_name' => 'a', 'user_id' => 'user-123', 'experiment_key' => 'checkout-flow'],
    ['event_name' => 'b', 'user_id' => 'user-123', 'feature_flag_key' => 'dark-mode'],
]);
check('trackBatch() returns a BatchResult mapped from the response', $r instanceof BatchResult && $r->successCount === 2 && $r->isOk());

$r = $client->trackBatch([['user_id' => 'u']]);
check('trackBatch() reports malformed events without sending them', $r->failureCount === 1 && ($r->errors[0]['index'] ?? null) === 0);

check('getAssignments()/getEvaluatedFlags() list the cache', count($client->getAssignments('user-123')) === 1 && $client->getEvaluatedFlags('user-123') === ['dark-mode']);

// Failures: never cached, never thrown
$http = new FakeHttpClient();
$http->on('POST', $assignPath, new ApiException('Experiment not found', 404));
$http->on('GET', $evaluatePath, new NetworkException('timeout'));
$http->on('POST', $trackPath, new RuntimeException('unexpected'));
$client = makeClient($http);

check('getAssignment() returns null on 404', $client->getAssignment('checkout-flow', 'user-123') === null);
$client->getAssignment('checkout-flow', 'user-123');
check('failed assignment is not cached (second request made)', count($http->requestsTo($assignPath)) === 2);
check('evaluateFlag() is disabled on a network error', !$client->evaluateFlag('dark-mode', 'user-123')->enabled);
try {
    $sent = $client->track('purchase', 'user-123', [], 'checkout-flow');
    check('track() never throws and returns false on error', $sent === false);
} catch (Throwable $e) {
    fail('track() never throws', 'threw ' . $e::class . ': ' . $e->getMessage());
}

// =============================================================================
// Summary
// =============================================================================

echo "\n" . str_repeat('=', 50) . "\n";
$total = $passed + count($failures);
echo "Results: $passed/$total tests passed\n";

if (empty($failures)) {
    echo "\nAll standalone tests PASSED\n";
    exit(0);
}

echo "\nFailed tests:\n";
foreach ($failures as $f) {
    echo "$f\n";
}
exit(1);
