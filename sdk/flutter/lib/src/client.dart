/// Main ExperimentationClient for the Flutter SDK.
library experimently_client;

import 'cache.dart';
import 'http_client.dart';
import 'models.dart';
import 'offline_store.dart';

/// Flutter/Dart client for Experimently.
///
/// ## How it works
/// - **The server decides.** [evaluateFlag] calls
///   `GET /api/v1/feature-flags/evaluate/{key}?user_id=…` and [getAssignment]
///   calls `POST /api/v1/tracking/assign`; nothing is bucketed on device.
/// - **Caching.** Successful results are cached in memory per user + key for
///   [SdkConfig.cacheTtl] and, when [SdkConfig.offlineFallback] is on, written
///   to the [OfflineStore]. Failures are never cached.
/// - **Failure.** [evaluateFlag] returns the cached value when present,
///   otherwise a disabled [EvalResult]; [getAssignment] returns the cached
///   value or `null`. Neither throws. A definitive HTTP 404 (unknown flag key,
///   or an experiment that is not ACTIVE) never falls back to a stale persisted value.
/// - **Tracking.** [track] never throws. With `experimentKey`/`featureFlagKey`
///   it sends one `POST /api/v1/tracking/track`; without keys it fans out via
///   `POST /api/v1/tracking/batch` to every cached assignment and evaluated
///   flag of the user (nothing cached → nothing sent).
///
/// ## Quickstart
/// ```dart
/// final client = ExperimentationClient(
///   config: SdkConfig(apiKey: 'your-api-key', baseUrl: 'http://localhost:8000'),
///   offlineStore: SharedPreferencesOfflineStore(), // Flutter apps; omit for plain Dart
/// );
/// await client.init();
///
/// final flag = await client.evaluateFlag('dark-mode', 'user-123');
/// final assignment = await client.getAssignment('checkout-experiment', 'user-123');
/// await client.track('purchase', 'user-123', experimentKey: 'checkout-experiment', value: 12.5);
/// await client.close();
/// ```
class ExperimentationClient {
  /// Maximum events per `POST /api/v1/tracking/batch` request.
  static const int batchLimit = 100;

  final SdkConfig _config;
  final EvaluationCache<EvalResult> _flagCache;
  final EvaluationCache<Assignment> _assignmentCache;
  final ApiHttpClient _http;
  final OfflineStore _offlineStore;

  final Map<String, Future<EvalResult>> _inflightFlags = {};
  final Map<String, Future<Assignment?>> _inflightAssignments = {};

  bool _initialised = false;

  ExperimentationClient({
    required SdkConfig config,
    ApiHttpClient? httpClient,
    OfflineStore? offlineStore,
  })  : _config = config,
        _flagCache = EvaluationCache<EvalResult>(ttl: config.cacheTtl),
        _assignmentCache = EvaluationCache<Assignment>(ttl: config.cacheTtl),
        _http = httpClient ??
            ApiHttpClient(
              apiKey: config.apiKey,
              baseUrl: config.baseUrl,
              timeout: config.timeout,
            ),
        _offlineStore = offlineStore ?? InMemoryOfflineStore();

  /// Initialises the client: opens the offline store when
  /// [SdkConfig.offlineFallback] is on. Makes **no** network request — flags
  /// are evaluated per user by the server, there is nothing to prefetch.
  ///
  /// Must be called once before using the client; further calls are no-ops.
  Future<void> init() async {
    if (_initialised) return;
    if (_config.offlineFallback) {
      await _offlineStore.init();
    }
    _initialised = true;
  }

  // ---------------------------------------------------------------------------
  // Feature flags
  // ---------------------------------------------------------------------------

  /// Evaluates [flagKey] for [userId] via
  /// `GET /api/v1/feature-flags/evaluate/{flagKey}?user_id={userId}`.
  ///
  /// Never throws: on failure returns the cached/offline value when present,
  /// otherwise [EvalResult.disabled]. [attributes] are accepted for API
  /// compatibility and not sent: the evaluate endpoint takes a context, which
  /// this SDK does not send yet.
  Future<EvalResult> evaluateFlag(
    String flagKey,
    String userId, {
    Map<String, dynamic>? attributes,
  }) async {
    _assertInitialised();

    final cached = _flagCache.get(userId, flagKey);
    if (cached != null) return cached;

    final inflightKey = '$userId\u0000$flagKey';
    final pending = _inflightFlags[inflightKey];
    if (pending != null) return pending;

    final future = _fetchFlag(flagKey, userId);
    _inflightFlags[inflightKey] = future;
    try {
      return await future;
    } finally {
      _inflightFlags.remove(inflightKey);
    }
  }

  Future<EvalResult> _fetchFlag(String flagKey, String userId) async {
    try {
      final json = await _http.get(
        '/api/v1/feature-flags/evaluate/${Uri.encodeComponent(flagKey)}',
        query: {'user_id': userId},
      );
      final result = EvalResult.fromJson(json, fallbackKey: flagKey);
      _flagCache.set(userId, flagKey, result);
      if (_config.offlineFallback) {
        await _offlineStore.setFlag(userId, result);
      }
      return result;
    } on ApiException catch (e) {
      if (e.isNotFound) {
        // Definitive answer from the server: never serve a stale value.
        if (_config.offlineFallback) {
          await _offlineStore.removeFlag(userId, flagKey);
        }
        return EvalResult.disabled(flagKey);
      }
      return _offlineFlag(flagKey, userId);
    } catch (_) {
      return _offlineFlag(flagKey, userId);
    }
  }

  EvalResult _offlineFlag(String flagKey, String userId) {
    if (_config.offlineFallback) {
      final stored = _offlineStore.getFlag(userId, flagKey);
      if (stored != null) return stored;
    }
    return EvalResult.disabled(flagKey);
  }

  /// Convenience: `(await evaluateFlag(...)).enabled`.
  Future<bool> isEnabled(String flagKey, String userId) async =>
      (await evaluateFlag(flagKey, userId)).enabled;

  // ---------------------------------------------------------------------------
  // Experiments
  // ---------------------------------------------------------------------------

  /// Returns the user's (sticky) assignment for [experimentKey] via
  /// `POST /api/v1/tracking/assign`; [attributes] are sent as `context`.
  ///
  /// Never throws. Returns `null` when the experiment is not ACTIVE / unknown
  /// (404) or when the request failed and no cached/offline value exists.
  Future<Assignment?> getAssignment(
    String experimentKey,
    String userId, {
    Map<String, dynamic>? attributes,
  }) async {
    _assertInitialised();

    final cached = _assignmentCache.get(userId, experimentKey);
    if (cached != null) return cached;

    final inflightKey = '$userId\u0000$experimentKey';
    final pending = _inflightAssignments[inflightKey];
    if (pending != null) return pending;

    final future = _fetchAssignment(experimentKey, userId, attributes);
    _inflightAssignments[inflightKey] = future;
    try {
      return await future;
    } finally {
      _inflightAssignments.remove(inflightKey);
    }
  }

  Future<Assignment?> _fetchAssignment(
    String experimentKey,
    String userId,
    Map<String, dynamic>? attributes,
  ) async {
    try {
      final json = await _http.post('/api/v1/tracking/assign', {
        'experiment_key': experimentKey,
        'user_id': userId,
        if (attributes != null) 'context': attributes,
      });
      final assignment = Assignment.fromJson(
        json,
        fallbackExperimentKey: experimentKey,
        fallbackUserId: userId,
      );
      _assignmentCache.set(userId, experimentKey, assignment);
      if (_config.offlineFallback) {
        await _offlineStore.setAssignment(userId, assignment);
      }
      return assignment;
    } on ApiException catch (e) {
      if (e.isNotFound) {
        if (_config.offlineFallback) {
          await _offlineStore.removeAssignment(userId, experimentKey);
        }
        return null;
      }
      return _offlineAssignment(experimentKey, userId);
    } catch (_) {
      return _offlineAssignment(experimentKey, userId);
    }
  }

  Assignment? _offlineAssignment(String experimentKey, String userId) {
    if (!_config.offlineFallback) return null;
    return _offlineStore.getAssignment(userId, experimentKey);
  }

  /// Convenience: the assigned `variantName`, or `null` when unassigned.
  Future<String?> getVariant(String experimentKey, String userId,
          {Map<String, dynamic>? attributes}) async =>
      (await getAssignment(experimentKey, userId, attributes: attributes))?.variantName;

  // ---------------------------------------------------------------------------
  // Tracking
  // ---------------------------------------------------------------------------

  /// Sends a tracking event. Fire-and-forget: **never throws**.
  ///
  /// - With [experimentKey] and/or [featureFlagKey]: one
  ///   `POST /api/v1/tracking/track`.
  /// - Without keys: one `POST /api/v1/tracking/batch` with one entry per
  ///   cached assignment and one per cached evaluated flag of [userId]. If
  ///   nothing is cached, nothing is sent.
  ///
  /// Returns `true` when every request succeeded (or nothing had to be sent).
  Future<bool> track(
    String eventName,
    String userId, {
    Map<String, dynamic>? properties,
    String? experimentKey,
    String? featureFlagKey,
    double? value,
    String? eventType,
    DateTime? timestamp,
  }) {
    return trackEvent(TrackEvent(
      userId: userId,
      eventName: eventName,
      properties: properties,
      experimentKey: experimentKey,
      featureFlagKey: featureFlagKey,
      value: value,
      eventType: eventType,
      timestamp: timestamp,
    ));
  }

  /// Same as [track] with a prebuilt [TrackEvent]. Never throws.
  Future<bool> trackEvent(TrackEvent event) async {
    _assertInitialised();
    if (event.hasKey) {
      try {
        await _http.post('/api/v1/tracking/track', event.toJson());
        return true;
      } catch (_) {
        return false;
      }
    }
    return _sendBatches(_fanOut(event));
  }

  /// Sends several events with `POST /api/v1/tracking/batch` (chunked by
  /// [batchLimit]). Keyed events are sent as-is; events without keys are fanned
  /// out to the user's cached assignments and flags. Never throws.
  ///
  /// Returns `true` when every batch request succeeded (or nothing had to be sent).
  Future<bool> trackBatch(List<TrackEvent> events) async {
    _assertInitialised();
    final expanded = <TrackEvent>[];
    for (final event in events) {
      if (event.hasKey) {
        expanded.add(event);
      } else {
        expanded.addAll(_fanOut(event));
      }
    }
    return _sendBatches(expanded);
  }

  List<TrackEvent> _fanOut(TrackEvent event) => [
        for (final a in getAssignments(event.userId)) event.attributed(experimentKey: a.experimentKey),
        for (final key in getEvaluatedFlags(event.userId)) event.attributed(featureFlagKey: key),
      ];

  Future<bool> _sendBatches(List<TrackEvent> events) async {
    if (events.isEmpty) return true;
    var ok = true;
    for (var i = 0; i < events.length; i += batchLimit) {
      final chunk = events.sublist(i, i + batchLimit > events.length ? events.length : i + batchLimit);
      try {
        await _http.post('/api/v1/tracking/batch', {
          'events': chunk.map((e) => e.toJson()).toList(),
        });
      } catch (_) {
        ok = false;
      }
    }
    return ok;
  }

  // ---------------------------------------------------------------------------
  // Cache access
  // ---------------------------------------------------------------------------

  /// Cached (successful, unexpired) assignments of [userId], in order.
  List<Assignment> getAssignments(String userId) =>
      _assignmentCache.entries(userId).map((e) => e.value).toList();

  /// Keys of flags successfully evaluated (and still cached) for [userId].
  List<String> getEvaluatedFlags(String userId) =>
      _flagCache.entries(userId).map((e) => e.key).toList();

  /// Clears the in-memory caches (the offline store is kept).
  void clearCache() {
    _flagCache.clear();
    _assignmentCache.clear();
  }

  /// Clears everything the SDK persisted to the offline store.
  Future<void> clearOfflineCache() => _offlineStore.clear();

  /// Releases resources held by this client (closes the HTTP connection pool).
  Future<void> close() async {
    clearCache();
    _http.close();
  }

  void _assertInitialised() {
    if (!_initialised) {
      throw StateError(
        'ExperimentationClient.init() must be called before using the client.',
      );
    }
  }
}
