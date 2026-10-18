/// SharedPreferences-backed [OfflineStore] (Flutter only).
library controlvar_shared_preferences_offline_store;

import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'models.dart';
import 'offline_store.dart';

/// Persists the last known server results to [SharedPreferences] so they can
/// be served offline across app launches.
///
/// All keys are namespaced under the `ep_sdk_` prefix; the user id is
/// length-prefixed so ids containing `:` cannot collide.
class SharedPreferencesOfflineStore implements OfflineStore {
  static const String _flagPrefix = 'ep_sdk_flag_';
  static const String _assignmentPrefix = 'ep_sdk_asgn_';

  SharedPreferences? _prefs;

  @override
  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();
  }

  static String _key(String prefix, String userId, String key) =>
      '$prefix${userId.length}:$userId:$key';

  @override
  Future<void> setFlag(String userId, EvalResult result) async {
    await _prefs?.setString(_key(_flagPrefix, userId, result.key), json.encode(result.toJson()));
  }

  @override
  EvalResult? getFlag(String userId, String flagKey) {
    final raw = _prefs?.getString(_key(_flagPrefix, userId, flagKey));
    if (raw == null) return null;
    try {
      return EvalResult.fromJson(
        Map<String, dynamic>.from(json.decode(raw) as Map),
        fallbackKey: flagKey,
      );
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> removeFlag(String userId, String flagKey) async {
    await _prefs?.remove(_key(_flagPrefix, userId, flagKey));
  }

  @override
  Future<void> setAssignment(String userId, Assignment assignment) async {
    await _prefs?.setString(
      _key(_assignmentPrefix, userId, assignment.experimentKey),
      json.encode(assignment.toJson()),
    );
  }

  @override
  Assignment? getAssignment(String userId, String experimentKey) {
    final raw = _prefs?.getString(_key(_assignmentPrefix, userId, experimentKey));
    if (raw == null) return null;
    try {
      return Assignment.fromJson(
        Map<String, dynamic>.from(json.decode(raw) as Map),
        fallbackExperimentKey: experimentKey,
        fallbackUserId: userId,
      );
    } catch (_) {
      return null;
    }
  }

  @override
  Future<void> removeAssignment(String userId, String experimentKey) async {
    await _prefs?.remove(_key(_assignmentPrefix, userId, experimentKey));
  }

  @override
  Future<void> clear() async {
    final keys = _prefs?.getKeys() ?? <String>{};
    for (final key in keys) {
      if (key.startsWith(_flagPrefix) || key.startsWith(_assignmentPrefix)) {
        await _prefs?.remove(key);
      }
    }
  }
}
