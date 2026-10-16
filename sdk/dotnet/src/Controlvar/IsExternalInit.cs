// C# 9 `init` accessors (used by SdkConfig) compile against a marker type that the BCL only ships
// from .NET 5 onwards. This internal polyfill makes the netstandard2.1 target build; it is
// excluded on net5.0+ where the framework already defines the type.
#if !NET5_0_OR_GREATER
namespace System.Runtime.CompilerServices
{
    /// <summary>Marker type required by the compiler for <c>init</c>-only setters.</summary>
    internal static class IsExternalInit
    {
    }
}
#endif
