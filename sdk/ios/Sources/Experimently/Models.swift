import Foundation

// MARK: - SDK Configuration

/// Configuration for the Experimently client.
public struct SdkConfig {
    /// Origin of the experimentation platform API (e.g. `"http://localhost:8000"`).
    /// The SDK appends `/api/v1/...` itself.
    public let baseURL: String
    /// API key sent as the `X-API-Key` header on every request.
    public let apiKey: String
    /// Network request timeout in seconds.
    public let timeout: TimeInterval
    /// Maximum number of cached evaluations / assignments kept in memory.
    public let cacheSize: Int
    /// Time-to-live for cached server results in seconds (default: 5 minutes).
    public let cacheTTL: TimeInterval
    /// When `true` (default) every successful server result is also persisted to `UserDefaults`
    /// and served when the network is unreachable.
    public let enableOfflineFallback: Bool

    public init(
        baseURL: String = "http://localhost:8000",
        apiKey: String,
        timeout: TimeInterval = 10.0,
        cacheSize: Int = 1000,
        cacheTTL: TimeInterval = 300,
        enableOfflineFallback: Bool = true
    ) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.timeout = timeout
        self.cacheSize = cacheSize
        self.cacheTTL = cacheTTL
        self.enableOfflineFallback = enableOfflineFallback
    }

    /// Source-compatibility initializer. Flags and experiments are now decided by the server, so
    /// `enableLocalEval` is ignored.
    @available(*, deprecated, message: "Flags are evaluated by the server; enableLocalEval is ignored.")
    public init(
        baseURL: String = "http://localhost:8000",
        apiKey: String,
        timeout: TimeInterval = 10.0,
        cacheSize: Int = 1000,
        cacheTTL: TimeInterval = 300,
        enableLocalEval: Bool
    ) {
        self.init(baseURL: baseURL, apiKey: apiKey, timeout: timeout, cacheSize: cacheSize, cacheTTL: cacheTTL)
    }
}

// MARK: - User

/// Represents a platform user for flag evaluation and experiment assignment.
public struct User: Codable {
    /// Unique identifier for the user.
    public let id: String
    /// Optional key-value attributes. They are sent as `context` when assigning the user to an
    /// experiment so the server can apply targeting rules.
    public let attributes: [String: AnyCodable]?

    public init(id: String, attributes: [String: Any]? = nil) {
        self.id = id
        self.attributes = attributes.map { dict in
            dict.mapValues { AnyCodable($0) }
        }
    }
}

// MARK: - Assignment

/// Result of assigning a user to an experiment variant (`POST /api/v1/tracking/assign`).
public struct Assignment: Codable {
    /// The experiment's unique string key.
    public let experimentKey: String
    /// The user ID used for the assignment.
    public let userId: String
    /// UUID of the assigned variant.
    public let variantId: String?
    /// Name of the assigned variant (e.g. `"control"`, `"treatment"`).
    public let variantName: String
    /// `true` when the user landed in the control variant.
    public let isControl: Bool
    /// The variant's `configuration` JSON from the experiment definition, if any.
    public let configuration: [String: Any]?

    /// Source-compatibility alias for ``variantName``.
    @available(*, deprecated, renamed: "variantName")
    public var variantKey: String { variantName }

    public init(
        experimentKey: String,
        userId: String,
        variantId: String?,
        variantName: String,
        isControl: Bool,
        configuration: [String: Any]? = nil
    ) {
        self.experimentKey = experimentKey
        self.userId = userId
        self.variantId = variantId
        self.variantName = variantName
        self.isControl = isControl
        self.configuration = configuration
    }

    enum CodingKeys: String, CodingKey {
        case experimentKey = "experiment_key"
        case userId = "user_id"
        case variantId = "variant_id"
        case variantName = "variant_name"
        case isControl = "is_control"
        case configuration
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        experimentKey = try container.decode(String.self, forKey: .experimentKey)
        userId = try container.decodeIfPresent(String.self, forKey: .userId) ?? ""
        variantId = try container.decodeIfPresent(String.self, forKey: .variantId)
        variantName = try container.decode(String.self, forKey: .variantName)
        isControl = try container.decodeIfPresent(Bool.self, forKey: .isControl) ?? false
        configuration = (try? container.decodeIfPresent([String: AnyCodable].self, forKey: .configuration))?
            .mapValues { $0.value }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(experimentKey, forKey: .experimentKey)
        try container.encode(userId, forKey: .userId)
        try container.encodeIfPresent(variantId, forKey: .variantId)
        try container.encode(variantName, forKey: .variantName)
        try container.encode(isControl, forKey: .isControl)
        try container.encodeIfPresent(configuration.map { $0.mapValues { AnyCodable($0) } }, forKey: .configuration)
    }
}

// MARK: - EvalResult

/// The result of evaluating a feature flag for a specific user
/// (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`).
public struct EvalResult: Codable {
    /// The evaluated flag key.
    public let key: String
    /// Whether the flag is enabled for this user (decided by the server).
    public let enabled: Bool
    /// `nil` today: the evaluate route returns `config: null` for every flag. It holds the
    /// JSON object if the server ever sends one, and is `nil` for `null` or a non-object value.
    public let config: [String: Any]?

    public init(key: String, enabled: Bool, config: [String: Any]? = nil) {
        self.key = key
        self.enabled = enabled
        self.config = config
    }

    enum CodingKeys: String, CodingKey {
        case key, enabled, config
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        key = try container.decodeIfPresent(String.self, forKey: .key) ?? ""
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
        config = (try? container.decodeIfPresent([String: AnyCodable].self, forKey: .config))?
            .mapValues { $0.value }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(key, forKey: .key)
        try container.encode(enabled, forKey: .enabled)
        try container.encodeIfPresent(config.map { $0.mapValues { AnyCodable($0) } }, forKey: .config)
    }
}

// MARK: - TrackEvent

/// An analytics event. Encodes to the wire format of `POST /api/v1/tracking/track`
/// (`properties` are sent as `metadata`; `eventType` defaults to `eventName`).
public struct TrackEvent: Codable {
    /// The ID of the user performing the event.
    public let userId: String
    /// The name of the event (e.g. `"purchase"`). Experiment metrics match on this name.
    public let eventName: String
    /// Optional event type; defaults to ``eventName`` on the wire.
    public let eventType: String?
    /// Attribute the event to one experiment. When neither this nor ``featureFlagKey`` is set,
    /// ``ExperimentationClient/track(_:)`` fans the event out to every cached assignment and flag.
    public let experimentKey: String?
    /// Attribute the event to one feature flag.
    public let featureFlagKey: String?
    /// Optional numeric value (revenue, duration, …).
    public let value: Double?
    /// Optional additional properties; sent as `metadata`.
    public let properties: [String: AnyCodable]?
    /// Optional event time; sent as ISO-8601. The server stamps the event when omitted.
    public let timestamp: Date?

    public init(
        userId: String,
        eventName: String,
        properties: [String: Any]? = nil,
        experimentKey: String? = nil,
        featureFlagKey: String? = nil,
        value: Double? = nil,
        eventType: String? = nil,
        timestamp: Date? = nil
    ) {
        self.userId = userId
        self.eventName = eventName
        self.eventType = eventType
        self.experimentKey = experimentKey
        self.featureFlagKey = featureFlagKey
        self.value = value
        self.properties = properties.map { dict in dict.mapValues { AnyCodable($0) } }
        self.timestamp = timestamp
    }

    /// `true` when the event is attributed to an experiment or a feature flag.
    public var hasKey: Bool { experimentKey != nil || featureFlagKey != nil }

    /// Returns a copy attributed to the given experiment and/or flag.
    public func attributed(experimentKey: String? = nil, featureFlagKey: String? = nil) -> TrackEvent {
        TrackEvent(copying: self, experimentKey: experimentKey, featureFlagKey: featureFlagKey)
    }

    private init(copying other: TrackEvent, experimentKey: String?, featureFlagKey: String?) {
        self.userId = other.userId
        self.eventName = other.eventName
        self.eventType = other.eventType
        self.experimentKey = experimentKey
        self.featureFlagKey = featureFlagKey
        self.value = other.value
        self.properties = other.properties
        self.timestamp = other.timestamp
    }

    enum CodingKeys: String, CodingKey {
        case eventType = "event_type"
        case eventName = "event_name"
        case userId = "user_id"
        case experimentKey = "experiment_key"
        case featureFlagKey = "feature_flag_key"
        case value
        case properties = "metadata"
        case timestamp
    }

    private static let iso8601: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        eventType = try container.decodeIfPresent(String.self, forKey: .eventType)
        eventName = try container.decodeIfPresent(String.self, forKey: .eventName) ?? eventType ?? ""
        userId = try container.decode(String.self, forKey: .userId)
        experimentKey = try container.decodeIfPresent(String.self, forKey: .experimentKey)
        featureFlagKey = try container.decodeIfPresent(String.self, forKey: .featureFlagKey)
        value = try container.decodeIfPresent(Double.self, forKey: .value)
        properties = try container.decodeIfPresent([String: AnyCodable].self, forKey: .properties)
        if let raw = try container.decodeIfPresent(String.self, forKey: .timestamp) {
            timestamp = TrackEvent.iso8601.date(from: raw)
                ?? ISO8601DateFormatter().date(from: raw)
        } else {
            timestamp = nil
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(eventType ?? eventName, forKey: .eventType)
        try container.encode(eventName, forKey: .eventName)
        try container.encode(userId, forKey: .userId)
        try container.encodeIfPresent(experimentKey, forKey: .experimentKey)
        try container.encodeIfPresent(featureFlagKey, forKey: .featureFlagKey)
        try container.encodeIfPresent(value, forKey: .value)
        try container.encodeIfPresent(properties, forKey: .properties)
        try container.encodeIfPresent(timestamp.map { TrackEvent.iso8601.string(from: $0) }, forKey: .timestamp)
    }
}

// MARK: - AnyCodable

/// A type-erased wrapper that supports encoding and decoding arbitrary JSON values.
/// Handles String, Int, Double, Bool, [Any], [String: Any], and nil.
public struct AnyCodable: Codable {
    /// The underlying value.
    public let value: Any

    public init(_ value: Any) {
        self.value = value
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()

        if container.decodeNil() {
            self.value = NSNull()
        } else if let bool = try? container.decode(Bool.self) {
            self.value = bool
        } else if let int = try? container.decode(Int.self) {
            self.value = int
        } else if let double = try? container.decode(Double.self) {
            self.value = double
        } else if let string = try? container.decode(String.self) {
            self.value = string
        } else if let array = try? container.decode([AnyCodable].self) {
            self.value = array.map { $0.value }
        } else if let dict = try? container.decode([String: AnyCodable].self) {
            self.value = dict.mapValues { $0.value }
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "AnyCodable: unsupported JSON value type"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()

        switch value {
        case is NSNull:
            try container.encodeNil()
        case let bool as Bool:
            try container.encode(bool)
        case let int as Int:
            try container.encode(int)
        case let double as Double:
            try container.encode(double)
        case let float as Float:
            try container.encode(Double(float))
        case let string as String:
            try container.encode(string)
        case let wrapped as AnyCodable:
            try wrapped.encode(to: encoder)
        case let array as [Any]:
            let wrapped = array.map { AnyCodable($0) }
            try container.encode(wrapped)
        case let dict as [String: Any]:
            let wrapped = dict.mapValues { AnyCodable($0) }
            try container.encode(wrapped)
        default:
            let context = EncodingError.Context(
                codingPath: encoder.codingPath,
                debugDescription: "AnyCodable: unsupported value type \(type(of: value))"
            )
            throw EncodingError.invalidValue(value, context)
        }
    }
}

// MARK: - ExperimentationError

/// Errors that can be thrown by the Experimently SDK.
public enum ExperimentationError: Error, LocalizedError {
    /// The SDK configuration is invalid.
    case invalidConfig(String)
    /// A network-level error occurred.
    case networkError(Error)
    /// The server response could not be decoded.
    case decodingError(Error)
    /// The requested feature flag key is unknown (HTTP 404).
    case flagNotFound(String)
    /// The requested experiment is unknown or not ACTIVE (HTTP 404).
    case experimentNotFound(String)
    /// The server returned an HTTP error response.
    case serverError(Int, String)
    /// The operation was cancelled.
    case cancelled

    public var errorDescription: String? {
        switch self {
        case .invalidConfig(let msg):
            return "Invalid SDK configuration: \(msg)"
        case .networkError(let err):
            return "Network error: \(err.localizedDescription)"
        case .decodingError(let err):
            return "Decoding error: \(err.localizedDescription)"
        case .flagNotFound(let key):
            return "Feature flag not found: '\(key)'"
        case .experimentNotFound(let key):
            return "Experiment not found or not active: '\(key)'"
        case .serverError(let code, let msg):
            return "Server error \(code): \(msg)"
        case .cancelled:
            return "Operation was cancelled"
        }
    }
}
