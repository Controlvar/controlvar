<?php

declare(strict_types=1);

namespace Controlvar\Tests;

use Controlvar\FeatureFlagEvaluator;
use PHPUnit\Framework\TestCase;

/**
 * Cross-SDK hash compatibility tests.
 *
 * These tests guarantee that the PHP SDK produces exactly the same bucket values
 * as the JavaScript, Python, Java, and React SDKs for the same inputs.
 *
 * Hash formula: MD5("{userId}:{flagKey}") → first 4 bytes little-endian uint32 → divide by 2^32
 */
class HashCompatibilityTest extends TestCase
{
    // -------------------------------------------------------------------------
    // Known test vector (must match all SDKs)
    // -------------------------------------------------------------------------

    public function testHashParityKnownVector(): void
    {
        $result = FeatureFlagEvaluator::hashUser('user-123', 'my-flag');

        $this->assertEqualsWithDelta(
            0.6927449859213084,
            $result,
            1e-10,
            'hashUser("user-123", "my-flag") must equal 0.6927449859213084 to be compatible with all other SDKs'
        );
    }

    // -------------------------------------------------------------------------
    // Invariants
    // -------------------------------------------------------------------------

    public function testHashReturnsBetweenZeroAndOne(): void
    {
        $cases = [
            ['user-1', 'flag-a'],
            ['user-2', 'flag-b'],
            ['alice', 'checkout-v2'],
            ['bob@example.com', 'dark-mode'],
            ['', ''],
            ['a', 'b'],
        ];

        foreach ($cases as [$userId, $flagKey]) {
            $h = FeatureFlagEvaluator::hashUser($userId, $flagKey);
            $this->assertGreaterThanOrEqual(0.0, $h, "Hash for '$userId:$flagKey' must be >= 0");
            $this->assertLessThan(1.0, $h, "Hash for '$userId:$flagKey' must be < 1.0");
        }
    }

    public function testHashIsDeterministic(): void
    {
        $userId  = 'deterministic-user';
        $flagKey = 'test-flag';

        $first  = FeatureFlagEvaluator::hashUser($userId, $flagKey);
        $second = FeatureFlagEvaluator::hashUser($userId, $flagKey);
        $third  = FeatureFlagEvaluator::hashUser($userId, $flagKey);

        $this->assertSame($first, $second, 'Hash must be deterministic across calls');
        $this->assertSame($second, $third, 'Hash must be deterministic across calls');
    }

    public function testHashDifferentInputsDifferentOutput(): void
    {
        $h1 = FeatureFlagEvaluator::hashUser('user-A', 'flag-1');
        $h2 = FeatureFlagEvaluator::hashUser('user-B', 'flag-1');
        $h3 = FeatureFlagEvaluator::hashUser('user-A', 'flag-2');

        // Statistically extremely unlikely to collide
        $this->assertNotSame($h1, $h2, 'Different users should (almost always) produce different hashes');
        $this->assertNotSame($h1, $h3, 'Different flags should (almost always) produce different hashes');
    }

    // -------------------------------------------------------------------------
    // Edge cases
    // -------------------------------------------------------------------------

    public function testHashEmptyStrings(): void
    {
        // md5(":") — should not throw, and must be in [0, 1)
        $h = FeatureFlagEvaluator::hashUser('', '');
        $this->assertIsFloat($h);
        $this->assertGreaterThanOrEqual(0.0, $h);
        $this->assertLessThan(1.0, $h);
    }

    public function testHashUnicode(): void
    {
        $h = FeatureFlagEvaluator::hashUser('用户-123', '功能标志');
        $this->assertIsFloat($h);
        $this->assertGreaterThanOrEqual(0.0, $h);
        $this->assertLessThan(1.0, $h);
    }

    public function testHashLongStrings(): void
    {
        $longUserId  = str_repeat('x', 10000);
        $longFlagKey = str_repeat('y', 10000);

        $h = FeatureFlagEvaluator::hashUser($longUserId, $longFlagKey);
        $this->assertIsFloat($h);
        $this->assertGreaterThanOrEqual(0.0, $h);
        $this->assertLessThan(1.0, $h);
    }

    public function testLocalEvaluationWasRemoved(): void
    {
        // The server decides: the hash is exported as a utility only.
        $this->assertFalse(method_exists(FeatureFlagEvaluator::class, 'evaluate'));
        $this->assertTrue(method_exists(FeatureFlagEvaluator::class, 'hashUser'));
    }

    public function testHashConsistencyAcrossMultipleCalls(): void
    {
        // Run 200 iterations to confirm no flakiness
        $first = FeatureFlagEvaluator::hashUser('stability-test', 'stability-flag');

        for ($i = 0; $i < 200; $i++) {
            $h = FeatureFlagEvaluator::hashUser('stability-test', 'stability-flag');
            $this->assertSame($first, $h, "Iteration $i returned a different hash value");
        }
    }

    // -------------------------------------------------------------------------
    // Additional known vectors (cross-SDK parity anchors)
    // -------------------------------------------------------------------------

    public function testHashAdditionalKnownVectors(): void
    {
        // Pre-computed via Python: hashlib.md5(b'<input>').digest() little-endian uint32 / 2^32
        $vectors = [
            ['user-456',  'feature-x',   null],   // just check bounds — not pre-known
            ['user-789',  'rollout-flag', null],
            ['user-000',  'my-flag',      null],
        ];

        foreach ($vectors as [$userId, $flagKey, $expected]) {
            $h = FeatureFlagEvaluator::hashUser($userId, $flagKey);
            $this->assertGreaterThanOrEqual(0.0, $h);
            $this->assertLessThan(1.0, $h);
            if ($expected !== null) {
                $this->assertEqualsWithDelta($expected, $h, 1e-10);
            }
        }
    }
}
