/// Offline fallback stores.
///
/// When [SdkConfig.offlineFallback] is true, [ExperimentationClient] writes
/// every successful server result here so it can be served when the API is
/// unreachable (airplane mode, connectivity loss, server errors).
///
/// This file is pure Dart. The SharedPreferences-backed implementation lives
/// in `shared_preferences_offline_store.dart` (Flutter only).
library controlvar_offline_store;

import 'models.dart';

/// Persistent (or session) store of the last known server results, keyed by
/// user + key. Entries have no TTL of their own: they stay until overwritten,
/// removed or cleared.
abstract class OfflineStore {
  /// Initialises the store. Must complete before any read/write.
  Future<void> init();

  /// Persists a flag evaluation for [userId] (keyed by `result.key`).
  Future<void> setFlag(String userId, EvalResult result);

  /// Retrieves the last persisted evaluation of [flagKey] for [userId], or `null`.
  EvalResult? getFlag(String userId, String flagKey);

  /// Removes the persisted evaluation of [flagKey] for [userId].
  Future<void> removeFlag(String userId, String flagKey);

  /// Persists an assignment for [userId] (keyed by `assignment.experimentKey`).
  Future<void> setAssignment(String userId, Assignment assignment);

  /// Retrieves the last persisted assignment of [experimentKey] for [userId], or `null`.
  Assignment? getAssignment(String userId, String experimentKey);

  /// Removes the persisted assignment of [experimentKey] for [userId].
  Future<void> removeAssignment(String userId, String experimentKey);

  /// Clears every entry written by this store.
  Future<void> clear();
}

/// Session-scoped [OfflineStore]: survives network loss within one process but
/// not an app restart. Default when no store is injected; also usable from
/// plain Dart (server-side, tests, the contract smoke).
class InMemoryOfflineStore implements OfflineStore {
  final Map<String, Map<String, EvalResult>> _flags = {};
  final Map<String, Map<String, Assignment>> _assignments = {};

  @override
  Future<void> init() async {}

  @override
  Future<void> setFlag(String userId, EvalResult result) async {
    _flags.putIfAbsent(userId, () => {})[result.key] = result;
  }

  @override
  EvalResult? getFlag(String userId, String flagKey) => _flags[userId]?[flagKey];

  @override
  Future<void> removeFlag(String userId, String flagKey) async {
    _flags[userId]?.remove(flagKey);
  }

  @override
  Future<void> setAssignment(String userId, Assignment assignment) async {
    _assignments.putIfAbsent(userId, () => {})[assignment.experimentKey] = assignment;
  }

  @override
  Assignment? getAssignment(String userId, String experimentKey) =>
      _assignments[userId]?[experimentKey];

  @override
  Future<void> removeAssignment(String userId, String experimentKey) async {
    _assignments[userId]?.remove(experimentKey);
  }

  @override
  Future<void> clear() async {
    _flags.clear();
    _assignments.clear();
  }
}
