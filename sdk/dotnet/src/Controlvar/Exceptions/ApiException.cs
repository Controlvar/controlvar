namespace Controlvar.Exceptions;

/// <summary>
/// Thrown when the API returns an error HTTP status code (4xx or 5xx, excluding 401).
/// </summary>
public class ApiException : ExperimentationException
{
    /// <summary>The HTTP status code returned by the API.</summary>
    public int StatusCode { get; }

    /// <summary>The raw response body, if available.</summary>
    public string? ResponseBody { get; }

    /// <summary>Creates the exception for an HTTP error status.</summary>
    /// <param name="statusCode">The HTTP status code.</param>
    /// <param name="message">Human-readable description.</param>
    /// <param name="responseBody">The raw response body, if any.</param>
    public ApiException(int statusCode, string message, string? responseBody = null)
        : base(message)
    {
        StatusCode = statusCode;
        ResponseBody = responseBody;
    }

    /// <summary>Creates the exception for an HTTP error status with an underlying cause (e.g. a JSON decode error).</summary>
    /// <param name="statusCode">The HTTP status code.</param>
    /// <param name="message">Human-readable description.</param>
    /// <param name="innerException">The underlying exception.</param>
    /// <param name="responseBody">The raw response body, if any.</param>
    public ApiException(int statusCode, string message, Exception innerException, string? responseBody = null)
        : base(message, innerException)
    {
        StatusCode = statusCode;
        ResponseBody = responseBody;
    }
}
