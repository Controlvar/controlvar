namespace Controlvar.Exceptions;

/// <summary>
/// Why a typed getter (<see cref="ExperimentationClient.GetStringValueAsync"/>,
/// <see cref="ExperimentationClient.GetNumberValueAsync"/>,
/// <see cref="ExperimentationClient.GetJsonValueAsync"/>) returned the caller's default although
/// the server answered, or was not asked: passed to <see cref="SdkConfig.OnError"/>, never thrown.
/// A failed request or an unknown flag (404) is passed on as the
/// <see cref="ExperimentationException"/> the request failed with instead (an
/// <see cref="ApiException"/> with its <see cref="ApiException.StatusCode"/>, a
/// <see cref="NetworkException"/>, an <see cref="AuthException"/>).
/// </summary>
public class FlagValueException : ExperimentationException
{
    /// <summary>The flag serves a value of another type than the getter reads.</summary>
    public const string TypeMismatch = "TYPE_MISMATCH";

    /// <summary>
    /// The answer has no value (a server from before flag values, or one that cannot serve the
    /// flag's stored value), or a value that is not of the type it says; or the getter was given a
    /// <c>null</c> or empty user id or flag key, and sent nothing.
    /// </summary>
    public const string InvalidResponse = "INVALID_RESPONSE";

    /// <summary>
    /// The reason as a code: <see cref="TypeMismatch"/> (<c>"TYPE_MISMATCH"</c>) or
    /// <see cref="InvalidResponse"/> (<c>"INVALID_RESPONSE"</c>), the codes the JavaScript SDK gives
    /// the same answers.
    /// </summary>
    public string Code { get; }

    /// <summary>Creates the exception.</summary>
    /// <param name="message">What happened, naming the flag and the getter.</param>
    /// <param name="code"><see cref="TypeMismatch"/> or <see cref="InvalidResponse"/>.</param>
    public FlagValueException(string message, string code) : base(message)
    {
        Code = code;
    }
}
