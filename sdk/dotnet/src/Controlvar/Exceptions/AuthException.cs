namespace Controlvar.Exceptions;

/// <summary>
/// Thrown when authentication fails (HTTP 401 Unauthorized).
/// </summary>
public class AuthException : ExperimentationException
{
    /// <summary>Creates the exception with a message.</summary>
    public AuthException(string message) : base(message) { }

    /// <summary>Creates the exception with a message and an underlying cause.</summary>
    public AuthException(string message, Exception innerException)
        : base(message, innerException) { }
}
