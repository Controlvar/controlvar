import Foundation

/// URLSession-based HTTP client for communicating with the Controlvar API.
///
/// Every request carries `X-API-Key`, `Accept: application/json` and
/// `Content-Type: application/json`. Throws `ExperimentationError` on network, server, or
/// decoding failures. Non-2xx responses surface as `.serverError(status, body)`.
public class HTTPClient {
    private let session: URLSession
    private let baseURL: String
    private let apiKey: String
    private let timeout: TimeInterval

    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    /// Characters left unescaped in path components and query values (RFC 3986 unreserved).
    private static let unreserved = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )

    /// - Parameters:
    ///   - baseURL: The origin of the API (e.g., `"http://localhost:8000"`).
    ///   - apiKey: API key sent in the `X-API-Key` request header.
    ///   - timeout: Request timeout in seconds (default: 10).
    ///   - session: URLSession to use. Inject a custom session for testing.
    public init(
        baseURL: String,
        apiKey: String,
        timeout: TimeInterval = 10.0,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.timeout = timeout
        self.session = session
    }

    // MARK: - Async/Await Interface

    /// Performs a GET request and decodes the response body as `T`.
    ///
    /// - Parameters:
    ///   - path: API path relative to `baseURL` (e.g., `/api/v1/feature-flags/evaluate/my-flag`).
    ///     Path segments must already be percent-encoded (see ``encodeComponent(_:)``).
    ///   - query: Query parameters; values are percent-encoded by the client.
    ///   - type: The `Decodable` type to decode the response into.
    public func get<T: Decodable>(_ path: String, query: [String: String] = [:], as type: T.Type) async throws -> T {
        let request = try buildRequest(path, query: query, method: "GET")
        let data = try await perform(request)
        return try decode(type, from: data)
    }

    /// Performs a POST request with a JSON-encoded body and decodes the response as `Response`.
    public func post<Body: Encodable, Response: Decodable>(
        _ path: String,
        body: Body,
        as type: Response.Type
    ) async throws -> Response {
        let bodyData = try encode(body)
        let request = try buildRequest(path, method: "POST", body: bodyData)
        let data = try await perform(request)
        return try decode(type, from: data)
    }

    /// Performs a POST request with a JSON-encoded body and ignores the response body.
    /// Still throws on network errors and non-2xx responses.
    public func post<Body: Encodable>(_ path: String, body: Body) async throws {
        let bodyData = try encode(body)
        let request = try buildRequest(path, method: "POST", body: bodyData)
        _ = try await perform(request)
    }

    /// Performs a fire-and-forget POST for event tracking.
    ///
    /// Launches a URLSession data task and returns immediately; errors are silently ignored.
    public func postFireAndForget<Body: Encodable>(_ path: String, body: Body) {
        guard let bodyData = try? encoder.encode(body),
              let request = try? buildRequest(path, method: "POST", body: bodyData)
        else { return }

        session.dataTask(with: request).resume()
    }

    // MARK: - Encoding helpers

    /// Percent-encodes a value for use as a single path segment or query value
    /// (everything except RFC 3986 unreserved characters is escaped).
    public static func encodeComponent(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: unreserved) ?? value
    }

    // MARK: - Private Helpers

    private func encode<Body: Encodable>(_ body: Body) throws -> Data {
        do {
            return try encoder.encode(body)
        } catch {
            throw ExperimentationError.decodingError(error)
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try decoder.decode(type, from: data)
        } catch {
            throw ExperimentationError.decodingError(error)
        }
    }

    private func perform(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse

        do {
            (data, response) = try await send(request)
        } catch let urlError as URLError where urlError.code == .cancelled {
            throw ExperimentationError.cancelled
        } catch {
            throw ExperimentationError.networkError(error)
        }

        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let message = String(data: data, encoding: .utf8) ?? "Unknown error"
            throw ExperimentationError.serverError(http.statusCode, message)
        }

        return data
    }

    /// Completion-handler based data task wrapped for async/await so the SDK keeps its
    /// iOS 14 / macOS 11 deployment target (`URLSession.data(for:)` needs iOS 15 / macOS 12).
    private func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            let task = session.dataTask(with: request) { data, response, error in
                if let error = error {
                    continuation.resume(throwing: error)
                    return
                }
                guard let response = response else {
                    continuation.resume(throwing: URLError(.badServerResponse))
                    return
                }
                continuation.resume(returning: (data ?? Data(), response))
            }
            task.resume()
        }
    }

    internal func buildRequest(
        _ path: String,
        query: [String: String] = [:],
        method: String,
        body: Data? = nil
    ) throws -> URLRequest {
        var urlString = baseURL.hasSuffix("/")
            ? String(baseURL.dropLast()) + path
            : baseURL + path

        if !query.isEmpty {
            let pairs = query
                .sorted { $0.key < $1.key }
                .map { "\(Self.encodeComponent($0.key))=\(Self.encodeComponent($0.value))" }
            urlString += (urlString.contains("?") ? "&" : "?") + pairs.joined(separator: "&")
        }

        guard let url = URL(string: urlString) else {
            throw ExperimentationError.invalidConfig("Invalid URL: \(urlString)")
        }

        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue(apiKey, forHTTPHeaderField: "X-API-Key")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        if let body = body {
            request.httpBody = body
        }

        return request
    }
}
