using System.Reflection;
using System.Runtime.Versioning;
using Controlvar;
using Xunit;

namespace Controlvar.Tests;

/// <summary>
/// Which build of the SDK a test run loaded, and on which runtime. These sources compile into
/// three legs: Controlvar.Tests on net8.0 and on net10.0, both against the SDK's net8.0 build,
/// and Controlvar.Tests.NetStandard on net8.0 against its netstandard2.1 build. A leg that
/// loaded the wrong build, or ran on another runtime, would otherwise pass every other test.
/// </summary>
public class TargetLegTests
{
#if CONTROLVAR_NETSTANDARD_LEG
    private const string ExpectedSdkBuild = ".NETStandard,Version=v2.1";
#else
    private const string ExpectedSdkBuild = ".NETCoreApp,Version=v8.0";
#endif

#if NET10_0_OR_GREATER
    private const int ExpectedRuntimeMajor = 10;
#else
    private const int ExpectedRuntimeMajor = 8;
#endif

    [Fact]
    public void LoadsTheSdkBuildItsLegIsFor()
    {
        var attribute = typeof(ExperimentationClient).Assembly.GetCustomAttribute<TargetFrameworkAttribute>();
        Assert.NotNull(attribute);
        Assert.Equal(ExpectedSdkBuild, attribute!.FrameworkName);
    }

    [Fact]
    public void RunsOnTheRuntimeItsLegIsFor()
    {
        Assert.Equal(ExpectedRuntimeMajor, Environment.Version.Major);
    }
}
