namespace Controlvar.Exceptions;

/// <summary>
/// Thrown when a network-level error occurs (e.g. connection refused, DNS failure, timeout).
/// </summary>
public class NetworkException : ExperimentationException
{
    /// <summary>Creates the exception with a message.</summary>
    public NetworkException(string message) : base(message) { }

    /// <summary>Creates the exception with a message and an underlying cause (the transport error).</summary>
    public NetworkException(string message, Exception innerException)
        : base(message, innerException) { }
}
