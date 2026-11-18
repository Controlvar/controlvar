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
use Controlvar\HttpClient;
use Controlvar\SdkConfig;
use PHPUnit\Framework\Attributes\Group;
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
    // evaluateFlag — the user's attributes as `context` (part of #1720)
    // -------------------------------------------------------------------------

    /** V0 and V1: no attributes, and an empty array. */
    private const NO_CONTEXT_PATH = '/api/v1/feature-flags/evaluate/f?user_id=u1';
    /** V2: {"plan":"pro"}, after user_id. */
    private const PLAN_PRO_PATH = '/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D';

    private function clientFor(HttpClient $http): ExperimentationClient
    {
        return new ExperimentationClient(
            new SdkConfig(baseUrl: 'https://api.example.com', apiKey: 'test-api-key', cacheTtl: 300, timeout: 5),
            $http
        );
    }

    #[Group('regression')]
    public function testEvaluateFlagSendsTheAttributesAsContextAfterUserId(): void
    {
        $server = new RuleServerHttpClient();
        $this->clientFor($server)->evaluateFlag('f', 'u1', ['plan' => 'pro']);

        $this->assertSame([self::PLAN_PRO_PATH], $server->paths);
    }

    #[Group('regression')]
    public function testEvaluateFlagMatchesATargetingRuleOnAnAttribute(): void
    {
        // The server enables the flag only when `context` decodes to plan = pro.
        $client = $this->clientFor(new RuleServerHttpClient());

        $this->assertTrue($client->evaluateFlag('f', 'u1', ['plan' => 'pro'])->enabled);
        $this->assertFalse($client->evaluateFlag('f', 'u2')->enabled, 'no attributes, no match');
    }

    public function testIsFeatureEnabledSendsTheAttributesToo(): void
    {
        $server = new RuleServerHttpClient();

        $this->assertTrue($this->clientFor($server)->isFeatureEnabled('f', 'u1', ['plan' => 'pro']));
        $this->assertSame([self::PLAN_PRO_PATH], $server->paths);
    }

    public function testTheContextDecodesToTheAttributesTheServerReads(): void
    {
        // The server reads a query by splitting on `&`, taking `+` as a space and
        // percent-decoding as UTF-8, then parses `context` as JSON. + & = # a space,
        // / and non-ASCII must arrive as the object that was sent.
        $vectors = [
            'V3'  => ['q' => "a+b&c=d#e f/é😀!'()*~"],
            'V3a' => ['q' => 'a+b&c=d#e fé'],
            'V4'  => [
                'age'    => 30,
                'beta'   => true,
                'device' => ['os' => 'ios', 'version' => '17.4.0'],
                'tags'   => ['a', 'b'],
            ],
        ];

        foreach ($vectors as $name => $attributes) {
            $server = new RuleServerHttpClient();
            $this->clientFor($server)->evaluateFlag('f', 'u1', $attributes);

            $this->assertCount(1, $server->paths, $name);
            $pairs = ServerQuery::pairs($server->paths[0]);
            $this->assertSame(['user_id', 'context'], array_column($pairs, 0), $name);
            $this->assertSame('u1', $pairs[0][1], $name);
            $this->assertSame($attributes, json_decode($pairs[1][1], true, 512, JSON_THROW_ON_ERROR), $name);
        }
    }

    public function testTheContextIsPinnedAsAnExactUrl(): void
    {
        // #1720's own vector, {"q":"a+b&c=d#e fé"}. The string is rawurlencode's output
        // for the compact JSON (RFC 3986, uppercase %XX), written out here and first run
        // by CI; the correctness check is the decode test above, not this copy.
        $server = new RuleServerHttpClient();
        $this->clientFor($server)->evaluateFlag('f', 'u1', ['q' => 'a+b&c=d#e fé']);

        $this->assertSame(
            ['/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%C3%A9%22%7D'],
            $server->paths
        );
    }

    public function testNoAttributesOrAnEmptyArraySendNoContext(): void
    {
        $none = new RuleServerHttpClient();
        $this->clientFor($none)->evaluateFlag('f', 'u1');
        $empty = new RuleServerHttpClient();
        $this->clientFor($empty)->evaluateFlag('f', 'u1', []);

        $this->assertSame([self::NO_CONTEXT_PATH], $none->paths, 'V0');
        $this->assertSame([self::NO_CONTEXT_PATH], $empty->paths, 'V1: [] is not sent as %5B%5D');
    }

    public function testAListShapedAttributesArrayGoesOutAsAJsonObject(): void
    {
        $server = new RuleServerHttpClient();
        $this->clientFor($server)->evaluateFlag('f', 'u1', ['a', 'b']);

        $this->assertSame('{"0":"a","1":"b"}', ServerQuery::value($server->paths[0], 'context'));
    }

    public function testTwoCallsThatDifferOnlyInAttributesGetTwoRequestsAndTwoAnswers(): void
    {
        $pairs = [
            'plan pro / plan free' => [['plan' => 'pro'], ['plan' => 'free']],
            'plan pro / tier pro'  => [['plan' => 'pro'], ['tier' => 'pro']],
            'n 1 / n "1"'          => [['n' => 1], ['n' => '1']],
        ];

        foreach ($pairs as $name => [$first, $second]) {
            $server = new RuleServerHttpClient();
            $client = $this->clientFor($server);

            $answers = [
                $client->evaluateFlag('f', 'u1', $first)->enabled,
                $client->evaluateFlag('f', 'u1', $second)->enabled,
            ];

            $this->assertSame([true, false], $answers, $name);
            $this->assertCount(2, $server->paths, $name);
        }
    }

    public function testOneCachedAnswerPerUserAndFlagSoABAMakesThreeRequests(): void
    {
        $server = new RuleServerHttpClient();
        $client = $this->clientFor($server);
        $a = ['plan' => 'pro'];
        $b = ['plan' => 'free'];

        $answers = [
            $client->evaluateFlag('f', 'u1', $a)->enabled,
            $client->evaluateFlag('f', 'u1', $b)->enabled,
            $client->evaluateFlag('f', 'u1', $a)->enabled,
        ];

        $this->assertSame([true, false, true], $answers);
        $this->assertCount(3, $server->paths, 'the slot holds B when A comes back');
    }

    public function testTheSameAttributesAreServedFromTheCache(): void
    {
        $server = new RuleServerHttpClient();
        $client = $this->clientFor($server);

        $client->evaluateFlag('f', 'u1', ['plan' => 'pro']);
        $client->evaluateFlag('f', 'u1', ['plan' => 'pro']);

        $this->assertCount(1, $server->paths);
    }

    public function testAnAnswerCachedWithAttributesIsNotServedToACallWithoutThem(): void
    {
        $server = new RuleServerHttpClient();
        $client = $this->clientFor($server);

        $this->assertTrue($client->evaluateFlag('f', 'u1', ['plan' => 'pro'])->enabled);
        $this->assertFalse($client->evaluateFlag('f', 'u1')->enabled);
        $this->assertSame([self::PLAN_PRO_PATH, self::NO_CONTEXT_PATH], $server->paths);
    }

    public function testNoAttributesAndAnEmptyArrayShareOneCachedAnswer(): void
    {
        $server = new RuleServerHttpClient();
        $client = $this->clientFor($server);

        $client->evaluateFlag('f', 'u1', []);
        $client->evaluateFlag('f', 'u1', []);
        $client->evaluateFlag('f', 'u1');

        $this->assertCount(1, $server->paths);
    }

    public function testTheSameAttributesInAnotherKeyOrderCostOneExtraRequest(): void
    {
        $server = new RuleServerHttpClient();
        $client = $this->clientFor($server);

        $client->evaluateFlag('f', 'u1', ['a' => 1, 'b' => 2]);
        $client->evaluateFlag('f', 'u1', ['b' => 2, 'a' => 1]);

        $this->assertCount(2, $server->paths);
    }

    public function testChangedAttributesStillListTheFlagOnceAndTrackFansOutOnce(): void
    {
        $server = new RuleServerHttpClient();
        $client = $this->clientFor($server);

        $client->evaluateFlag('f', 'u1', ['plan' => 'pro']);
        $client->evaluateFlag('f', 'u1', ['plan' => 'free']);

        $this->assertSame(['f'], $client->getEvaluatedFlags('u1'));
        $this->assertTrue($client->track('page_view', 'u1'));
        $this->assertCount(1, $server->posts);
        $this->assertSame(self::BATCH_PATH, $server->posts[0]['path']);
        $events = $server->posts[0]['body']['events'];
        $this->assertCount(1, $events);
        $this->assertSame('f', $events[0]['feature_flag_key']);
    }

    public function testAFailedEvaluationWithAttributesIsNotCached(): void
    {
        $http = new FakeHttpClient();
        $http->on('GET', self::PLAN_PRO_PATH, new ApiException('boom', 500));
        $client = $this->clientFor($http);

        $this->assertFalse($client->evaluateFlag('f', 'u1', ['plan' => 'pro'])->enabled);
        $this->assertFalse($client->evaluateFlag('f', 'u1', ['plan' => 'pro'])->enabled);

        $this->assertCount(2, $http->requests);
        $this->assertSame([], $client->getEvaluatedFlags('u1'));
    }

    public function testEvaluateFlagSendsNothingWhenTheAttributesCannotBeWrittenAsJson(): void
    {
        $canary = 'attrcanary7f3';
        $cases = [
            'invalid UTF-8' => ['plan' => $canary . "\xFF"],
            'NAN'           => ['n' => NAN, 'plan' => $canary],
            'INF'           => ['n' => INF, 'plan' => $canary],
            'an object that refuses' => ['o' => new class ($canary) implements \JsonSerializable {
                public function __construct(private readonly string $canary)
                {
                }

                public function jsonSerialize(): mixed
                {
                    throw new \RuntimeException($this->canary);
                }
            }],
        ];

        foreach ($cases as $name => $attributes) {
            $server = new RuleServerHttpClient();
            $client = $this->clientFor($server);

            ob_start();
            $result = $client->evaluateFlag('f', 'u1', $attributes);
            $output = (string) ob_get_clean();

            $this->assertFalse($result->enabled, $name);
            $this->assertSame('f', $result->key, $name);
            $this->assertSame([], $server->paths, "$name: no request is made");
            $this->assertSame([], $client->getEvaluatedFlags('u1'), "$name: nothing is cached");
            $this->assertSame('', $output, "$name: nothing is printed");
            $this->assertStringNotContainsString($canary, (string) json_encode($result->toArray()), $name);

            // Nothing was cached for the slot, so the next call, with sendable
            // attributes, asks the server.
            $this->assertTrue($client->evaluateFlag('f', 'u1', ['plan' => 'pro'])->enabled, $name);
            $this->assertCount(1, $server->paths, $name);
        }
    }

    public function testNoOutputOrLogNamesTheAttributesWhenARequestFails(): void
    {
        $canary = 'attrcanary7f3';
        // An HttpClient that fails the way cURL can, with the request path (query
        // included) in the exception's text: the probe, so that a canary in the output
        // or the log would show.
        $http = new class (new SdkConfig(baseUrl: 'https://api.example.com', apiKey: 'k')) extends HttpClient {
            /** @var list<string> */
            public array $messages = [];

            public function get(string $path, array $headers = []): array
            {
                $error = new NetworkException('cURL error: failed for ' . $path);
                $this->messages[] = $error->getMessage();
                throw $error;
            }
        };
        $client = $this->clientFor($http);

        $log = (string) tempnam(sys_get_temp_dir(), 'a1php');
        $oldLog = ini_set('error_log', $log);
        $oldLogErrors = ini_set('log_errors', '1');
        ob_start();
        try {
            $flag = $client->evaluateFlag('f', 'u1', ['plan' => $canary]);
            $enabled = $client->isFeatureEnabled('f', 'u1', ['plan' => $canary]);
        } finally {
            $output = (string) ob_get_clean();
            ini_set('error_log', (string) $oldLog);
            ini_set('log_errors', (string) $oldLogErrors);
        }
        $logged = (string) file_get_contents($log);
        unlink($log);

        $this->assertCount(2, $http->messages);
        $this->assertStringContainsString($canary, $http->messages[0], 'the probe: the exception text did carry the query');
        $this->assertFalse($flag->enabled);
        $this->assertFalse($enabled);
        $this->assertSame('', $output);
        $this->assertSame('', $logged);
        $this->assertStringNotContainsString($canary, (string) json_encode($flag->toArray()));
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

/**
 * How the server reads a request path's query: split on `&`, then on the first `=`,
 * `+` is a space, then percent-decode as UTF-8. This is the decode helper of the
 * attribute tests. `urldecode` reads `+` as a space and `rawurldecode` does not;
 * the server (Starlette) does, so a client that leaves `+` raw reads as a space here.
 */
final class ServerQuery
{
    /**
     * @return list<array{0: string, 1: string}> (name, value) pairs in order of appearance
     */
    public static function pairs(string $path): array
    {
        $mark = strpos($path, '?');
        if ($mark === false) {
            return [];
        }

        $pairs = [];
        foreach (explode('&', substr($path, $mark + 1)) as $part) {
            [$name, $value] = array_pad(explode('=', $part, 2), 2, '');
            $pairs[] = [self::decode($name), self::decode($value)];
        }

        return $pairs;
    }

    public static function value(string $path, string $name): string
    {
        foreach (self::pairs($path) as [$pairName, $value]) {
            if ($pairName === $name) {
                return $value;
            }
        }

        throw new \OutOfBoundsException("no {$name} parameter in the query");
    }

    private static function decode(string $part): string
    {
        $decoded = urldecode($part);
        if (preg_match('//u', $decoded) !== 1) {
            throw new \UnexpectedValueException('the query does not decode to valid UTF-8');
        }

        return $decoded;
    }
}

/**
 * A fake server for the attribute tests. It records the raw path it is handed (the
 * capture point), and answers a flag evaluation as the real route does for a rule on
 * `plan` (or on the number `n`): enabled only when `context` decodes, with the
 * server's rules, to an object with plan = "pro" (n = 1). A `context` that is not a
 * JSON object is a 422, as on the server.
 */
final class RuleServerHttpClient extends HttpClient
{
    private const EVALUATE_PREFIX = '/api/v1/feature-flags/evaluate/';

    /** @var list<string> */
    public array $paths = [];

    /** @var list<array{path: string, body: array<mixed>}> */
    public array $posts = [];

    public function __construct()
    {
        parent::__construct(new SdkConfig(baseUrl: 'https://api.example.com', apiKey: 'test-api-key'));
    }

    public function get(string $path, array $headers = []): array
    {
        $this->paths[] = $path;

        $context = [];
        foreach (ServerQuery::pairs($path) as [$name, $value]) {
            if ($name !== 'context') {
                continue;
            }
            try {
                $context = json_decode($value, true, 512, JSON_THROW_ON_ERROR);
            } catch (\JsonException) {
                $context = null;
            }
            if (!is_array($context)) {
                throw new ApiException('Query parameter context must be a JSON object', 422);
            }
        }

        $key = rawurldecode(substr(explode('?', $path, 2)[0], strlen(self::EVALUATE_PREFIX)));
        $enabled = ($context['plan'] ?? null) === 'pro' || ($context['n'] ?? null) === 1;

        return ['key' => $key, 'enabled' => $enabled, 'config' => null];
    }

    public function post(string $path, array $body, array $headers = []): array
    {
        $this->posts[] = ['path' => $path, 'body' => $body];

        return ['success_count' => count($body['events'] ?? []), 'failure_count' => 0, 'errors' => null];
    }
}
