using Controlvar;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// Verifies that the .NET SDK's consistent hash function produces values identical to all
/// other SDKs in the platform (JavaScript, Python, Java, Go, etc.).
/// </summary>
public class HashCompatibilityTests
{
    // Cross-SDK canonical test vector.
    private const double KnownVectorResult = 0.6927449859213084;
    private const double Epsilon = 1e-10;

    [Fact]
    public void HashUser_KnownVector_ReturnsExpectedValue()
    {
        double result = FeatureFlagEvaluator.HashUser("user-123", "my-flag");
        Assert.Equal(KnownVectorResult, result, precision: 10);
    }

    [Fact]
    public void HashUser_Result_IsWithinZeroToOne_Exclusive()
    {
        double result = FeatureFlagEvaluator.HashUser("user-abc", "flag-xyz");
        Assert.True(result >= 0.0, $"Expected >= 0.0 but got {result}");
        Assert.True(result < 1.0, $"Expected < 1.0 but got {result}");
    }

    [Fact]
    public void HashUser_SameInputs_ProducesSameResult()
    {
        double first = FeatureFlagEvaluator.HashUser("user-999", "feature-flag");
        double second = FeatureFlagEvaluator.HashUser("user-999", "feature-flag");
        Assert.Equal(first, second);
    }

    [Fact]
    public void HashUser_DifferentUsers_ProduceDifferentResults()
    {
        double a = FeatureFlagEvaluator.HashUser("user-001", "same-flag");
        double b = FeatureFlagEvaluator.HashUser("user-002", "same-flag");
        // Different users will almost certainly (deterministically) produce different buckets.
        Assert.NotEqual(a, b);
    }

    [Fact]
    public void HashUser_DifferentFlags_ProduceDifferentResults()
    {
        double a = FeatureFlagEvaluator.HashUser("same-user", "flag-a");
        double b = FeatureFlagEvaluator.HashUser("same-user", "flag-b");
        Assert.NotEqual(a, b);
    }

    [Fact]
    public void HashUser_EmptyStrings_DoesNotThrow()
    {
        // Empty strings are valid inputs; they should hash consistently.
        var ex = Record.Exception(() => FeatureFlagEvaluator.HashUser("", ""));
        Assert.Null(ex);
    }

    [Fact]
    public void HashUser_EmptyStrings_ResultIsInRange()
    {
        double result = FeatureFlagEvaluator.HashUser("", "");
        Assert.True(result >= 0.0 && result < 1.0);
    }

    [Fact]
    public void HashUser_UnicodeInput_DoesNotThrow()
    {
        var ex = Record.Exception(() => FeatureFlagEvaluator.HashUser("用户-123", "标志-abc"));
        Assert.Null(ex);
    }

    [Fact]
    public void HashUser_UnicodeInput_ResultIsInRange()
    {
        double result = FeatureFlagEvaluator.HashUser("用户-123", "标志-abc");
        Assert.True(result >= 0.0 && result < 1.0);
    }

    [Fact]
    public void HashUser_LongInput_DoesNotThrow()
    {
        string longUserId = new string('x', 10_000);
        string longFlagKey = new string('y', 10_000);
        var ex = Record.Exception(() => FeatureFlagEvaluator.HashUser(longUserId, longFlagKey));
        Assert.Null(ex);
    }

    [Fact]
    public void HashUser_LongInput_ResultIsInRange()
    {
        string longUserId = new string('x', 10_000);
        string longFlagKey = new string('y', 10_000);
        double result = FeatureFlagEvaluator.HashUser(longUserId, longFlagKey);
        Assert.True(result >= 0.0 && result < 1.0);
    }

    [Fact]
    public void HashUser_MultipleCalls_AreConsistentAcrossCalls()
    {
        // Verify determinism across many repeated calls.
        double first = FeatureFlagEvaluator.HashUser("user-123", "my-flag");
        for (int i = 0; i < 100; i++)
        {
            double result = FeatureFlagEvaluator.HashUser("user-123", "my-flag");
            Assert.Equal(first, result);
        }
    }

    [Fact]
    public void HashUser_KnownVectorPrecise_MatchesWithinEpsilon()
    {
        double result = FeatureFlagEvaluator.HashUser("user-123", "my-flag");
        Assert.True(Math.Abs(result - KnownVectorResult) < Epsilon,
            $"Expected {KnownVectorResult} but got {result} (diff: {Math.Abs(result - KnownVectorResult)})");
    }
}
