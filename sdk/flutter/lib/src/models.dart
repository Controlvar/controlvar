/// Core data models for the Controlvar Flutter SDK.
library controlvar_models;

/// Configuration for the [ExperimentationClient].
class SdkConfig {
  /// API key sent as `X-API-Key` on every request.
  final String apiKey;

  /// Origin of the Controlvar API, e.g. `https://api.example.com`.
  /// The SDK appends `/api/v1/...` itself.
  final String baseUrl;

  /// HTTP request timeout. Defaults to 5 seconds.
  final Duration timeout;

  /// TTL for cached server results (per user + key). Defaults to 5 minutes.
  final Duration cacheTtl;

  /// When true, every successful evaluation/assignment is also written to the
  /// client's [OfflineStore] and served when the API is unreachable.
  final bool offlineFallback;

  const SdkConfig({
    required this.apiKey,
    required this.baseUrl,
    this.timeout = const Duration(seconds: 5),
    this.cacheTtl = const Duration(minutes: 5),
    this.offlineFallback = true,
  });
}

/// Server decision for one feature flag
/// (`GET /api/v1/feature-flags/evaluate/{key}?user_id=…`).
class EvalResult {
  /// The evaluated flag key.
  final String key;

  /// Whether the flag is enabled for the user (decided by the server).
  final bool enabled;

  /// `null` today: the evaluate route returns `config: null` for every flag. It
  /// holds the JSON value if the server ever sends one.
  final dynamic config;

  const EvalResult({required this.key, required this.enabled, this.config});

  /// The idiomatic "unknown" result returned when a request fails and nothing
  /// is cached.
  factory EvalResult.disabled(String key) => EvalResult(key: key, enabled: false);

  factory EvalResult.fromJson(Map<String, dynamic> json, {String? fallbackKey}) {
    return EvalResult(
      key: (json['key'] as String?) ?? fallbackKey ?? '',
      enabled: json['enabled'] == true,
      config: json['config'],
    );
  }

  Map<String, dynamic> toJson() => {
        'key': key,
        'enabled': enabled,
        'config': config,
      };

  /// [config] as a map when the server returned a JSON object, else `null`.
  Map<String, dynamic>? get configMap =>
      config is Map ? Map<String, dynamic>.from(config as Map) : null;

  @override
  String toString() => 'EvalResult(key: $key, enabled: $enabled, config: $config)';
}

/// Assignment of a user to an experiment variant
/// (`POST /api/v1/tracking/assign`). Sticky on the server.
class Assignment {
  final String experimentKey;
  final String userId;

  /// UUID of the assigned variant.
  final String? variantId;

  /// Name of the assigned variant, e.g. `control` or `treatment`.
  final String variantName;

  /// `true` when the user landed in the control variant.
  final bool isControl;

  /// The variant's `configuration` JSON from the experiment definition.
  final Map<String, dynamic>? configuration;

  const Assignment({
    required this.experimentKey,
    required this.userId,
    required this.variantName,
    this.variantId,
    this.isControl = false,
    this.configuration,
  });

  /// Source-compatibility alias for [variantName].
  @Deprecated('Use variantName')
  String get variantKey => variantName;

  factory Assignment.fromJson(
    Map<String, dynamic> json, {
    String? fallbackExperimentKey,
    String? fallbackUserId,
  }) {
    final rawConfig = json['configuration'];
    return Assignment(
      experimentKey: (json['experiment_key'] as String?) ?? fallbackExperimentKey ?? '',
      userId: (json['user_id'] as String?) ?? fallbackUserId ?? '',
      variantId: json['variant_id']?.toString(),
      variantName: (json['variant_name'] as String?) ?? '',
      isControl: json['is_control'] == true,
      configuration: rawConfig is Map ? Map<String, dynamic>.from(rawConfig) : null,
    );
  }

  Map<String, dynamic> toJson() => {
        'experiment_key': experimentKey,
        'user_id': userId,
        'variant_id': variantId,
        'variant_name': variantName,
        'is_control': isControl,
        'configuration': configuration,
      };

  @override
  String toString() =>
      'Assignment(experimentKey: $experimentKey, variantName: $variantName, isControl: $isControl)';
}

/// An analytics event. [toJson] produces the body of
/// `POST /api/v1/tracking/track` (and of each `/tracking/batch` entry).
class TrackEvent {
  final String userId;

  /// Event name, e.g. `purchase`. Experiment metrics match on this name.
  final String eventName;

  /// Defaults to [eventName] on the wire.
  final String? eventType;

  /// Attribute the event to one experiment. Without [experimentKey] and
  /// [featureFlagKey] the client fans the event out to every cached
  /// assignment and flag of the user.
  final String? experimentKey;

  /// Attribute the event to one feature flag.
  final String? featureFlagKey;

  /// Optional numeric value (revenue, duration, …).
  final double? value;

  /// Optional properties; sent as `metadata`.
  final Map<String, dynamic>? properties;

  /// Optional event time; sent as ISO-8601 (UTC).
  final DateTime? timestamp;

  const TrackEvent({
    required this.userId,
    required this.eventName,
    this.eventType,
    this.experimentKey,
    this.featureFlagKey,
    this.value,
    this.properties,
    this.timestamp,
  });

  /// Whether the event is attributed to an experiment or a flag. Empty keys
  /// count as absent (the server rejects them with 422).
  bool get hasKey => _present(experimentKey) || _present(featureFlagKey);

  static bool _present(String? key) => key != null && key.isNotEmpty;

  /// Copy attributed to the given experiment and/or flag.
  TrackEvent attributed({String? experimentKey, String? featureFlagKey}) => TrackEvent(
        userId: userId,
        eventName: eventName,
        eventType: eventType,
        experimentKey: experimentKey,
        featureFlagKey: featureFlagKey,
        value: value,
        properties: properties,
        timestamp: timestamp,
      );

  Map<String, dynamic> toJson() => {
        'event_type': eventType ?? eventName,
        'event_name': eventName,
        'user_id': userId,
        if (_present(experimentKey)) 'experiment_key': experimentKey,
        if (_present(featureFlagKey)) 'feature_flag_key': featureFlagKey,
        if (value != null) 'value': value,
        if (properties != null) 'metadata': properties,
        if (timestamp != null) 'timestamp': timestamp!.toUtc().toIso8601String(),
      };
}
