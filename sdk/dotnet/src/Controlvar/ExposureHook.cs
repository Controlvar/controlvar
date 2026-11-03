using System.Runtime.CompilerServices;
using Controlvar.Models;

namespace Controlvar;

/// <summary>
/// The one path between <see cref="SdkConfig.OnExposure"/> and the client: nothing the function
/// does reaches the client's own code, so it can never change or fail an answer. Not public API.
/// </summary>
internal static class ExposureHook
{
    /// <summary>The <see cref="ArgumentException"/> message for an async function.</summary>
    internal const string AsyncRefused =
        "OnExposure must not be async: an async lambda or method runs as async void, and an " +
        "exception after its first await would end the process. Write the exposure to a queue " +
        "instead, such as a Channel.";

    /// <summary>
    /// Throws <see cref="ArgumentException"/> when any function in <paramref name="hook"/> is an
    /// async lambda or <c>async</c> method. Every function combined into the delegate is checked,
    /// not only the last one, which is all <see cref="Delegate.Method"/> names. A synchronous
    /// lambda that itself calls an <c>async void</c> method cannot be told apart and is accepted.
    /// </summary>
    internal static void RefuseAsync(Action<Exposure> hook, string paramName)
    {
        foreach (var function in hook.GetInvocationList())
        {
            if (function.Method.IsDefined(typeof(AsyncStateMachineAttribute), inherit: false))
                throw new ArgumentException(AsyncRefused, paramName);
        }
    }

    /// <summary>
    /// Calls each function in <paramref name="hook"/> in turn with <paramref name="exposure"/>, on
    /// this thread, and drops whatever any of them throws: this SDK has no error channel, so a
    /// failing function changes nothing and is reported nowhere. One that throws does not stop
    /// the next.
    /// </summary>
    internal static void Call(Action<Exposure> hook, Exposure exposure)
    {
        foreach (var function in hook.GetInvocationList())
        {
            try
            {
                ((Action<Exposure>)function)(exposure);
            }
            catch (Exception)
            {
                // Dropped on purpose (see the summary). Nothing here reads the exception, so a
                // Message or ToString that throws cannot escape either.
            }
        }
    }
}
