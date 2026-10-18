<?php

declare(strict_types=1);

namespace Controlvar\Tests;

use Controlvar\Assignment;
use Controlvar\BatchResult;
use Controlvar\Errors\ApiException;
use Controlvar\Errors\AuthException;
use Controlvar\Errors\NetworkException;
use Controlvar\ExperimentationClient;
use Controlvar\FlagEvaluation;
use Controlvar\SdkConfig;
use PHPUnit\Framework\TestCase;

/**
 * Unit tests for ExperimentationClient against the public API contract.
 *
 * The HttpClient is replaced by FakeHttpClient (tests/FakeHttpClient.php) so
 * every request (method, path, JSON body) can be asserted without a network.
 */
class ExperimentationClientTest extends TestCase
{
    private const ASSIGN_PATH   = '/api/v1/tracking/assign';
    private const TRACK_PATH    = '/api/v1/tracking/track';
    private const BATCH_PATH    = '/api/v1/tracking/batch';
    private const EVALUATE_PATH = '/api/v1/feature-flags/evaluate/dark-mode?user_id=user-123';

    private FakeHttpClient $http;

    protected function setUp(): void
    {
        $this->http = new FakeHttpClient();
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    private function makeClient(int $cacheTtl = 300): ExperimentationClient
    {
        $config = new SdkConfig(
            baseUrl: 'https://api.example.com',
            apiKey: 'test-api-key',
            cacheTtl: $cacheTtl,
            timeout: 5,
        );

        return new ExperimentationClient($config, $this->http);
    }

    /**
     * @return array<string, mixed>
     */
    private function assignResponse(array $overrides = []): array
    {
        return array_merge([
            'experiment_key' => 'checkout-flow',
            'user_id'        => 'user-123',
            'variant_id'     => 'var-uuid-1',
            'variant_name'   => 'treatment',
            'is_control'     => false,
            'configuration'  => ['color' => 'green'],
        ], $overrides);
    }

    /**
     * @return array<string, mixed>
     */
    private function evaluateResponse(array $overrides = []): array
    {
        return array_merge(['key' => 'dark-mode', 'enabled' => true, 'config' => ['theme' => 'dark']], $overrides);
    }

    private function stubAssign(): void
    {
        $this->http->on('POST', self::ASSIGN_PATH, $this->assignResponse());
    }

    private function stubEvaluate(): void
    {
        $this->http->on('GET', self::EVALUATE_PATH, $this->evaluateResponse());
    }

    private function stubBatchOk(int $count = 2): void
    {
        $this->http->on('POST', self::BATCH_PATH, ['success_count' => $count, 'failure_count' => 0, 'errors' => null]);
    }

    // -------------------------------------------------------------------------
    // Constructor
    // -------------------------------------------------------------------------

    public function testConstructorCreatesDefaultHttpClientWhenNotProvided(): void
    {
        $client = new ExperimentationClient(new SdkConfig(baseUrl: 'https://api.example.com', apiKey: 'k'));
        $this->assertInstanceOf(ExperimentationClient::class, $client);
    }

    // -------------------------------------------------------------------------
    // getAssignment — POST /api/v1/tracking/assign
    // -------------------------------------------------------------------------

    public function testGetAssignmentPostsToTrackingAssignWithJsonBody(): void
    {
        $this->stubAssign();
        $this->makeClient()->getAssignment('checkout-flow', 'user-123', ['country' => 'US']);

        $this->assertCount(1, $this->http->requests);
        $request = $this->http->requests[0];
        $this->assertSame('POST', $request['method']);
        $this->assertSame(self::ASSIGN_PATH, $request['path']);
        $this->assertSame(
            ['experiment_key' => 'checkout-flow', 'user_id' => 'user-123', 'context' => ['country' => 'US']],
            $request['body']
        );
    }

    public function testGetAssignmentOmitsContextWhenNoAttributes(): void
    {
        $this->stubAssign();
        $this->makeClient()->getAssignment('checkout-flow', 'user-123');

        $this->assertSame(
            ['experiment_key' => 'checkout-flow', 'user_id' => 'user-123'],
            $this->http->requests[0]['body']
        );
    }

    public function testGetAssignmentMapsResponseToAssignment(): void
    {
        $this->stubAssign();
        $assignment = $this->makeClient()->getAssignment('checkout-flow', 'user-123');

        $this->assertInstanceOf(Assignment::class, $assignment);
        $this->assertSame('checkout-flow', $assignment->experimentKey);
        $this->assertSame('var-uuid-1', $assignment->variantId);
        $this->assertSame('treatment', $assignment->variantName);
        $this->assertFalse($assignment->isControl);
        $this->assertSame(['color' => 'green'], $assignment->configuration);
        $this->assertSame('treatment', $assignment->toArray()['variant_name']);
    }

    public function testGetAssignmentMapsControlVariant(): void
    {
        $this->http->on('POST', self::ASSIGN_PATH, [
            'variant_id' => 'v0', 'variant_name' => 'control', 'is_control' => true, 'configuration' => null,
        ]);
        $assignment = $this->makeClient()->getAssignment('checkout-flow', 'user-123');

        $this->assertNotNull($assignment);
        $this->assertTrue($assignment->isControl);
        $this->assertSame('checkout-flow', $assignment->experimentKey, 'falls back to the requested key');
        $this->assertNull($assignment->configuration);
    }

    public function testGetAssignmentIsStickyViaCache(): void
    {
        $this->stubAssign();
        $client = $this->makeClient();

        $first  = $client->getAssignment('checkout-flow', 'user-123');
        $second = $client->getAssignment('checkout-flow', 'user-123');

        $this->assertSame($first, $second);
        $this->assertCount(1, $this->http->requests, 'second call must be served from the cache');
    }

    public function testGetAssignmentCachesPerUser(): void
    {
        $this->stubAssign();
        $client = $this->makeClient();

        $client->getAssignment('checkout-flow', 'user-123');
        $client->getAssignment('checkout-flow', 'user-456');

        $this->assertCount(2, $this->http->requests);
        $this->assertSame('user-456', $this->http->requests[1]['body']['user_id']);
    }

    public function testGetAssignmentReturnsNullOn404AndNeverCachesFailure(): void
    {
        $this->http->on('POST', self::ASSIGN_PATH, new ApiException('Experiment not found', 404));
        $client = $this->makeClient();

        $this->assertNull($client->getAssignment('checkout-flow', 'user-123'));
        $this->assertNull($client->getAssignment('checkout-flow', 'user-123'));
        $this->assertCount(2, $this->http->requests, 'failures must not be cached');
        $this->assertSame([], $client->getAssignments('user-123'));
    }

    public function testGetAssignmentReturnsNullOnAuthError(): void
    {
        $this->http->on('POST', self::ASSIGN_PATH, new AuthException());
        $this->assertNull($this->makeClient()->getAssignment('checkout-flow', 'user-123'));
    }

    public function testGetAssignmentReturnsNullOnNetworkError(): void
    {
        $this->http->on('POST', self::ASSIGN_PATH, new NetworkException('timeout'));
        $this->assertNull($this->makeClient()->getAssignment('checkout-flow', 'user-123'));
    }

    public function testGetAssignmentReturnsNullWhenResponseHasNoVariantName(): void
    {
        $this->http->on('POST', self::ASSIGN_PATH, ['user_id' => 'user-123']);
        $this->assertNull($this->makeClient()->getAssignment('checkout-flow', 'user-123'));
    }

    // -------------------------------------------------------------------------
    // evaluateFlag — GET /api/v1/feature-flags/evaluate/{key}?user_id=…
    // -------------------------------------------------------------------------

    public function testEvaluateFlagGetsEvaluateEndpointWithUserIdQuery(): void
    {
        $this->stubEvaluate();
        $this->makeClient()->evaluateFlag('dark-mode', 'user-123');

        $this->assertCount(1, $this->http->requests);
        $this->assertSame('GET', $this->http->requests[0]['method']);
        $this->assertSame(self::EVALUATE_PATH, $this->http->requests[0]['path']);
        $this->assertNull($this->http->requests[0]['body']);
    }

    public function testEvaluateFlagPercentEncodesKeyAndUserId(): void
    {
        $path = '/api/v1/feature-flags/evaluate/my%20flag%2Fx?user_id=user%20one%26two';
        $this->http->on('GET', $path, ['key' => 'my flag/x', 'enabled' => true, 'config' => null]);

        $result = $this->makeClient()->evaluateFlag('my flag/x', 'user one&two');

        $this->assertTrue($result->enabled);
        $this->assertSame($path, $this->http->requests[0]['path']);
    }

    public function testEvaluateFlagMapsResponseToFlagEvaluation(): void
    {
        $this->stubEvaluate();
        $result = $this->makeClient()->evaluateFlag('dark-mode', 'user-123');

        $this->assertInstanceOf(FlagEvaluation::class, $result);
        $this->assertSame('dark-mode', $result->key);
        $this->assertTrue($result->enabled);
        $this->assertSame(['theme' => 'dark'], $result->config);
        $this->assertSame(['key' => 'dark-mode', 'enabled' => true, 'config' => ['theme' => 'dark']], $result->toArray());
    }

    public function testEvaluateFlagReportsDisabledFlagWithNullConfig(): void
    {
        $this->http->on('GET', self::EVALUATE_PATH, ['key' => 'dark-mode', 'enabled' => false, 'config' => null]);
        $result = $this->makeClient()->evaluateFlag('dark-mode', 'user-123');

        $this->assertFalse($result->enabled);
        $this->assertNull($result->config);
    }

    public function testEvaluateFlagTreatsNonBooleanEnabledAsDisabled(): void
    {
        $this->http->on('GET', self::EVALUATE_PATH, ['key' => 'dark-mode', 'enabled' => 'yes']);
        $this->assertFalse($this->makeClient()->evaluateFlag('dark-mode', 'user-123')->enabled);
    }

    public function testEvaluateFlagCachesPerUserAndKey(): void
    {
        $this->stubEvaluate();
        $client = $this->makeClient();

        $client->evaluateFlag('dark-mode', 'user-123');
        $client->evaluateFlag('dark-mode', 'user-123');
        $client->evaluateFlag('dark-mode', 'user-123');

        $this->assertCount(1, $this->http->requests);
    }

    public function testEvaluateFlagRequestsAgainForAnotherUser(): void
    {
        $this->stubEvaluate();
        $other = '/api/v1/feature-flags/evaluate/dark-mode?user_id=user-456';
        $this->http->on('GET', $other, $this->evaluateResponse());
        $client = $this->makeClient();

        $client->evaluateFlag('dark-mode', 'user-123');
        $client->evaluateFlag('dark-mode', 'user-456');

        $this->assertCount(2, $this->http->requests);
        $this->assertSame($other, $this->http->requests[1]['path']);
    }

    public function testEvaluateFlagExpiresCachedEvaluationAfterTtl(): void
    {
        $this->stubEvaluate();
        $client = $this->makeClient(cacheTtl: 1);

        $client->evaluateFlag('dark-mode', 'user-123');
        sleep(2);
        $client->evaluateFlag('dark-mode', 'user-123');

        $this->assertCount(2, $this->http->requests, 'expired entry must trigger a new request');
    }

    public function testEvaluateFlagReportsDisabledOn404AndNeverCachesFailure(): void
    {
        $this->http->on('GET', self::EVALUATE_PATH, new ApiException('Feature flag not found', 404));
        $client = $this->makeClient();

        $result = $client->evaluateFlag('dark-mode', 'user-123');
        $this->assertFalse($result->enabled);
        $this->assertSame('dark-mode', $result->key);
        $this->assertNull($result->config);

        $client->evaluateFlag('dark-mode', 'user-123');
        $this->assertCount(2, $this->http->requests);
        $this->assertSame([], $client->getEvaluatedFlags('user-123'));
    }

    public function testEvaluateFlagReportsDisabledOnNetworkError(): void
    {
        $this->http->on('GET', self::EVALUATE_PATH, new NetworkException('timeout'));
        $this->assertFalse($this->makeClient()->evaluateFlag('dark-mode', 'user-123')->enabled);
    }

    public function testIsFeatureEnabledReturnsServerDecision(): void
    {
        $this->stubEvaluate();
        $this->assertTrue($this->makeClient()->isFeatureEnabled('dark-mode', 'user-123'));
    }

    public function testIsFeatureEnabledReturnsFalseOnFailure(): void
    {
        $this->http->on('GET', self::EVALUATE_PATH, new ApiException('boom', 500));
        $this->assertFalse($this->makeClient()->isFeatureEnabled('dark-mode', 'user-123'));
    }

    // -------------------------------------------------------------------------
    // track — with a key: POST /api/v1/tracking/track
    // -------------------------------------------------------------------------

    public function testTrackWithExperimentKeyPostsOneTrackRequest(): void
    {
        $this->http->on('POST', self::TRACK_PATH, ['id' => 'evt-1']);

        $ok = $this->makeClient()->track('purchase', 'user-123', ['sku' => 'pro'], 'checkout-flow', null, 12.5);

        $this->assertTrue($ok);
        $this->assertCount(1, $this->http->requests);
        $request = $this->http->requests[0];
        $this->assertSame('POST', $request['method']);
        $this->assertSame(self::TRACK_PATH, $request['path']);

        $body = $request['body'];
        $this->assertSame('purchase', $body['event_type']);
        $this->assertSame('purchase', $body['event_name']);
        $this->assertSame('user-123', $body['user_id']);
        $this->assertSame('checkout-flow', $body['experiment_key']);
        $this->assertSame(12.5, $body['value']);
        $this->assertSame(['sku' => 'pro'], $body['metadata']);
        $this->assertMatchesRegularExpression('/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/', $body['timestamp']);
        $this->assertArrayNotHasKey('feature_flag_key', $body);
        $this->assertSame([], $this->http->requestsTo(self::BATCH_PATH));
    }

    public function testTrackWithFeatureFlagKeyAndCustomEventType(): void
    {
        $this->http->on('POST', self::TRACK_PATH, []);

        $ok = $this->makeClient()->track('search', 'user-123', [], null, 'new-search', null, 'interaction');

        $this->assertTrue($ok);
        $body = $this->http->requests[0]['body'];
        $this->assertSame('interaction', $body['event_type']);
        $this->assertSame('search', $body['event_name']);
        $this->assertSame('new-search', $body['feature_flag_key']);
        $this->assertArrayNotHasKey('experiment_key', $body);
        $this->assertArrayNotHasKey('value', $body);
        $this->assertArrayNotHasKey('metadata', $body);
    }

    public function testTrackReturnsFalseOnApiError(): void
    {
        $this->http->on('POST', self::TRACK_PATH, new ApiException('Internal Server Error', 500));
        $this->assertFalse($this->makeClient()->track('purchase', 'user-123', [], 'checkout-flow'));
    }

    public function testTrackReturnsFalseOnNetworkError(): void
    {
        $this->http->on('POST', self::TRACK_PATH, new NetworkException('timeout'));
        $this->assertFalse($this->makeClient()->track('purchase', 'user-123', [], 'checkout-flow'));
    }

    public function testTrackReturnsFalseOnAuthError(): void
    {
        $this->http->on('POST', self::TRACK_PATH, new AuthException());
        $this->assertFalse($this->makeClient()->track('purchase', 'user-123', [], 'checkout-flow'));
    }

    public function testTrackNeverThrows(): void
    {
        $this->http->on('POST', self::TRACK_PATH, new \RuntimeException('Unexpected error'));
        $this->assertFalse($this->makeClient()->track('crash-event', 'user-123', [], 'checkout-flow'));
    }

    // -------------------------------------------------------------------------
    // track — without a key: fan-out via POST /api/v1/tracking/batch
    // -------------------------------------------------------------------------

    public function testTrackWithoutKeySendsNothingWhenNothingIsCached(): void
    {
        $this->assertTrue($this->makeClient()->track('page_view', 'user-123'));
        $this->assertSame([], $this->http->requests);
    }

    public function testTrackWithoutKeyFansOutToCachedAssignmentAndFlag(): void
    {
        $this->stubAssign();
        $this->stubEvaluate();
        $this->stubBatchOk();
        $client = $this->makeClient();

        $client->getAssignment('checkout-flow', 'user-123');
        $client->evaluateFlag('dark-mode', 'user-123');

        $this->assertTrue($client->track('page_view', 'user-123', ['page' => '/']));

        $batches = $this->http->requestsTo(self::BATCH_PATH);
        $this->assertCount(1, $batches);
        $events = $batches[0]['body']['events'];
        $this->assertCount(2, $events);

        $this->assertSame('checkout-flow', $events[0]['experiment_key']);
        $this->assertArrayNotHasKey('feature_flag_key', $events[0]);
        $this->assertSame('dark-mode', $events[1]['feature_flag_key']);
        $this->assertArrayNotHasKey('experiment_key', $events[1]);

        foreach ($events as $event) {
            $this->assertSame('page_view', $event['event_type']);
            $this->assertSame('page_view', $event['event_name']);
            $this->assertSame('user-123', $event['user_id']);
            $this->assertSame(['page' => '/'], $event['metadata']);
        }
        $this->assertSame([], $this->http->requestsTo(self::TRACK_PATH));
    }

    public function testTrackWithoutKeyOnlyFansOutToThatUsersCache(): void
    {
        $this->stubAssign();
        $client = $this->makeClient();
        $client->getAssignment('checkout-flow', 'user-123');

        $this->assertTrue($client->track('page_view', 'user-456'));
        $this->assertSame([], $this->http->requestsTo(self::BATCH_PATH));
    }

    public function testTrackWithoutKeyDoesNotFanOutToFailedEvaluations(): void
    {
        $this->stubAssign();
        $this->http->on('GET', self::EVALUATE_PATH, new ApiException('not found', 404));
        $this->stubBatchOk(1);
        $client = $this->makeClient();

        $client->getAssignment('checkout-flow', 'user-123');
        $client->evaluateFlag('dark-mode', 'user-123');
        $client->track('page_view', 'user-123');

        $events = $this->http->requestsTo(self::BATCH_PATH)[0]['body']['events'];
        $this->assertCount(1, $events);
        $this->assertSame('checkout-flow', $events[0]['experiment_key']);
    }

    public function testTrackWithoutKeyReturnsFalseWhenBatchFails(): void
    {
        $this->stubAssign();
        $this->http->on('POST', self::BATCH_PATH, new ApiException('boom', 500));
        $client = $this->makeClient();
        $client->getAssignment('checkout-flow', 'user-123');

        $this->assertFalse($client->track('page_view', 'user-123'));
    }

    public function testTrackWithoutKeyNeverThrows(): void
    {
        $this->stubAssign();
        $this->http->on('POST', self::BATCH_PATH, new \RuntimeException('unexpected'));
        $client = $this->makeClient();
        $client->getAssignment('checkout-flow', 'user-123');

        $this->assertFalse($client->track('page_view', 'user-123'));
    }

    // -------------------------------------------------------------------------
    // trackBatch — POST /api/v1/tracking/batch
    // -------------------------------------------------------------------------

    public function testTrackBatchPostsEventsAndMapsResponse(): void
    {
        $this->stubBatchOk();

        $result = $this->makeClient()->trackBatch([
            ['event_name' => 'purchase', 'user_id' => 'user-123', 'experiment_key' => 'checkout-flow', 'value' => 12.5],
            ['event_name' => 'search', 'user_id' => 'user-123', 'feature_flag_key' => 'new-search', 'properties' => ['q' => 'shoes']],
        ]);

        $this->assertInstanceOf(BatchResult::class, $result);
        $this->assertSame(2, $result->successCount);
        $this->assertSame(0, $result->failureCount);
        $this->assertNull($result->errors);
        $this->assertTrue($result->isOk());

        $batches = $this->http->requestsTo(self::BATCH_PATH);
        $this->assertCount(1, $batches);
        $events = $batches[0]['body']['events'];
        $this->assertCount(2, $events);
        $this->assertSame('checkout-flow', $events[0]['experiment_key']);
        $this->assertSame(12.5, $events[0]['value']);
        $this->assertSame('purchase', $events[0]['event_type']);
        $this->assertSame('new-search', $events[1]['feature_flag_key']);
        $this->assertSame(['q' => 'shoes'], $events[1]['metadata']);
    }

    public function testTrackBatchChunksInto100AndAggregatesCounts(): void
    {
        $this->http
            ->on('POST', self::BATCH_PATH, ['success_count' => 100, 'failure_count' => 0, 'errors' => null])
            ->on('POST', self::BATCH_PATH, ['success_count' => 49, 'failure_count' => 1, 'errors' => [['index' => 3, 'error' => 'bad']]]);

        $events = [];
        for ($i = 1; $i <= 150; $i++) {
            $events[] = ['event_name' => 'e', 'user_id' => "u{$i}", 'experiment_key' => 'x'];
        }

        $result = $this->makeClient()->trackBatch($events);

        $batches = $this->http->requestsTo(self::BATCH_PATH);
        $this->assertCount(2, $batches);
        $this->assertCount(100, $batches[0]['body']['events']);
        $this->assertCount(50, $batches[1]['body']['events']);
        $this->assertSame(149, $result->successCount);
        $this->assertSame(1, $result->failureCount);
        $this->assertSame([['index' => 3, 'error' => 'bad']], $result->errors);
    }

    public function testTrackBatchCountsEveryEventAsFailedOnNetworkErrorAndNeverThrows(): void
    {
        $this->http->on('POST', self::BATCH_PATH, new NetworkException('refused'));

        $result = $this->makeClient()->trackBatch([['event_name' => 'e', 'user_id' => 'u', 'experiment_key' => 'x']]);

        $this->assertSame(1, $result->failureCount);
        $this->assertFalse($result->isOk());
        $this->assertNotNull($result->errors);
        $this->assertArrayHasKey('error', $result->errors[0]);
    }

    public function testTrackBatchReportsMalformedEventsWithoutSendingThem(): void
    {
        $this->http->on('POST', self::BATCH_PATH, ['success_count' => 1, 'failure_count' => 0]);

        $result = $this->makeClient()->trackBatch([
            ['user_id' => 'u'],
            ['event_name' => 'e', 'user_id' => 'u', 'experiment_key' => 'x'],
        ]);

        $this->assertSame(1, $result->successCount);
        $this->assertSame(1, $result->failureCount);
        $this->assertSame(0, $result->errors[0]['index']);
        $this->assertCount(1, $this->http->requestsTo(self::BATCH_PATH)[0]['body']['events']);
    }

    public function testTrackBatchSendsNothingForAnEmptyList(): void
    {
        $result = $this->makeClient()->trackBatch([]);

        $this->assertTrue($result->isOk());
        $this->assertSame([], $this->http->requests);
    }

    // -------------------------------------------------------------------------
    // Cache helpers
    // -------------------------------------------------------------------------

    public function testGetAssignmentsListsCachedAssignmentsForTheUser(): void
    {
        $this->stubAssign();
        $client = $this->makeClient();
        $client->getAssignment('checkout-flow', 'user-123');

        $keys = array_map(static fn (Assignment $a): string => $a->experimentKey, $client->getAssignments('user-123'));
        $this->assertSame(['checkout-flow'], $keys);
        $this->assertSame([], $client->getAssignments('user-456'));
    }

    public function testGetEvaluatedFlagsListsCachedFlagKeysForTheUser(): void
    {
        $this->stubEvaluate();
        $client = $this->makeClient();
        $client->evaluateFlag('dark-mode', 'user-123');

        $this->assertSame(['dark-mode'], $client->getEvaluatedFlags('user-123'));
        $this->assertSame([], $client->getEvaluatedFlags('user-456'));
    }

    public function testClearCacheDropsCachedResults(): void
    {
        $this->stubEvaluate();
        $client = $this->makeClient();

        $client->evaluateFlag('dark-mode', 'user-123');
        $client->clearCache();
        $client->evaluateFlag('dark-mode', 'user-123');

        $this->assertCount(2, $this->http->requests);
    }
}
