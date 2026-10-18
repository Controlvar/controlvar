namespace Controlvar.Exceptions;

/// <summary>
/// Base exception for all Controlvar SDK errors.
/// </summary>
public class ExperimentationException : Exception
{
    /// <summary>Creates the exception with a message.</summary>
    public ExperimentationException(string message) : base(message) { }

    /// <summary>Creates the exception with a message and an underlying cause.</summary>
    public ExperimentationException(string message, Exception innerException)
        : base(message, innerException) { }
}
