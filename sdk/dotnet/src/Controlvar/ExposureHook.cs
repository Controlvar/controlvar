using System.Runtime.CompilerServices;
using Controlvar.Exceptions;
using Controlvar.Models;

namespace Controlvar;

/// <summary>
/// The one path between the functions an integrator gives the client
/// (<see cref="SdkConfig.OnExposure"/>, <see cref="SdkConfig.OnError"/>) and the client: nothing a
/// function does reaches the client's own code, so it can never change or fail an answer. Not
/// public API.
/// </summary>
internal static class ExposureHook
{
    /// <summary>The <see cref="ArgumentException"/> message for an async function.</summary>
    internal const string AsyncRefused =
        "OnExposure must not be async: an async lambda or method runs as async void, and an " +
        "exception after its first await would end the process. Write the exposure to a queue " +
        "instead, such as a Channel.";

    /// <summary>The <see cref="ArgumentException"/> message for an async <see cref="SdkConfig.OnError"/>.</summary>
    internal const string AsyncOnErrorRefused =
        "OnError must not be async: an async lambda or method runs as async void, and an " +
        "exception after its first await would end the process. Write the error to a queue " +
        "instead, such as a Channel.";

    /// <summary>
    /// Throws <see cref="ArgumentException"/> with <paramref name="message"/> when any function in
    /// <paramref name="hook"/> is an async lambda or <c>async</c> method. Every function combined
    /// into the delegate is checked, not only the last one, which is all
    /// <see cref="Delegate.Method"/> names. A synchronous lambda that itself calls an
    /// <c>async void</c> method cannot be told apart and is accepted.
    /// </summary>
    internal static void RefuseAsync(Delegate hook, string paramName, string message = AsyncRefused)
    {
        foreach (var function in hook.GetInvocationList())
        {
            if (function.Method.IsDefined(typeof(AsyncStateMachineAttribute), inherit: false))
                throw new ArgumentException(message, paramName);
        }
    }

    /// <summary>
    /// Calls each function in <paramref name="hook"/> in turn with <paramref name="exposure"/>, on
    /// this thread, and drops whatever any of them throws: <see cref="SdkConfig.OnError"/> hears the
    /// typed getters' fallbacks only, so a failing function changes nothing and is reported
    /// nowhere. One that throws does not stop the next.
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

    /// <summary>
    /// Calls each function in <paramref name="hook"/> in turn with <paramref name="error"/> and
    /// <paramref name="operation"/>, on this thread, and drops whatever any of them throws: there is
    /// no channel left to report it to. One that throws does not stop the next.
    /// </summary>
    internal static void CallOnError(Action<ExperimentationException, string> hook, ExperimentationException error, string operation)
    {
        foreach (var function in hook.GetInvocationList())
        {
            try
            {
                ((Action<ExperimentationException, string>)function)(error, operation);
            }
            catch (Exception)
            {
                // Dropped on purpose (see the summary), as in Call.
            }
        }
    }
}
