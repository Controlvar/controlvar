// Contract smoke for the Flutter/Dart SDK.
//
// Run from the repo root against a seeded backend (see backend/scripts/seed_sdk_contract.py):
//
//     cd sdk/flutter && CONTROLVAR_API_KEY=... dart run example/contract_smoke.dart
//
// Needs the Flutter toolchain (the package depends on the `flutter` SDK); run
// `flutter pub get` once first so `dart run` does not print resolution output.
//
// Environment: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY
// (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY (default
// sdk_contract_flag), CONTRACT_USER_ID (default smoke-<uuid>).
//
// Prints exactly one JSON line on stdout and exits 0; on failure prints one line to stderr and
// exits 1. Uses only the SDK's public (Flutter-free) API.

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:controlvar/controlvar_core.dart';

String _env(String name, String defaultValue) {
  final value = Platform.environment[name];
  return (value == null || value.isEmpty) ? defaultValue : value;
}

Never _fail(String message) {
  stderr.writeln('flutter contract smoke: ${message.replaceAll('\n', ' ')}');
  exit(1);
}

/// Random RFC 4122 version-4 UUID (no `uuid` package needed).
String _uuidV4() {
  final rng = Random.secure();
  final bytes = List<int>.generate(16, (_) => rng.nextInt(256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-'
      '${hex.substring(16, 20)}-${hex.substring(20)}';
}

Future<void> main() async {
  try {
    await _run();
  } catch (e) {
    _fail('$e');
  }
  exit(0);
}

Future<void> _run() async {
  final apiUrl =
      _env('CONTROLVAR_API_URL', _env('EXPERIMENTLY_API_URL', 'http://localhost:8000'));
  final apiKey = _env('CONTROLVAR_API_KEY', _env('EXPERIMENTLY_API_KEY', ''));
  if (apiKey.isEmpty) _fail('CONTROLVAR_API_KEY is required');
  final experimentKey = _env('CONTRACT_EXPERIMENT_KEY', 'sdk_contract_ab');
  final flagKey = _env('CONTRACT_FLAG_KEY', 'sdk_contract_flag');
  final userId = _env('CONTRACT_USER_ID', 'smoke-${_uuidV4()}');

  final config = SdkConfig(
    apiKey: apiKey,
    baseUrl: apiUrl,
    timeout: const Duration(seconds: 10),
    offlineFallback: false,
  );
  final attributes = <String, dynamic>{'source': 'contract_smoke', 'sdk': 'flutter'};

  // `client` keeps the assignment and flag cached (needed for the fan-out step);
  // `fresh` starts with an empty cache so the second assignment is a real round-trip
  // that proves the assignment is sticky on the server.
  final client = ExperimentationClient(config: config);
  final fresh = ExperimentationClient(config: config);
  await client.init();
  await fresh.init();

  try {
    // 1. Assign twice → identical (sticky on the server).
    final first = await client.getAssignment(experimentKey, userId, attributes: attributes);
    if (first == null) {
      _fail('assignment failed for $experimentKey (unknown experiment key, bad API key, or API unreachable)');
    }
    final second = await fresh.getAssignment(experimentKey, userId, attributes: attributes);
    if (second == null) _fail('second assignment failed for $experimentKey');
    if (!const {'control', 'treatment'}.contains(first.variantName)) {
      _fail("unexpected variant_name '${first.variantName}' for $experimentKey");
    }
    final sticky = first.variantName == second.variantName && first.variantId == second.variantId;
    if (!sticky) {
      _fail('assignment not sticky: ${first.variantName}/${first.variantId} '
          'vs ${second.variantName}/${second.variantId}');
    }

    // 2. Evaluate the flag → enabled is a bool (seeded flag is 100% on).
    //    evaluateFlag never throws, so a failure shows up as "nothing cached".
    final flag = await client.evaluateFlag(flagKey, userId);
    if (!client.getEvaluatedFlags(userId).contains(flagKey)) {
      _fail('flag evaluation failed for $flagKey (unknown flag key, bad key, or API unreachable)');
    }

    // 3. Track `purchase` with a value and the experiment key → POST /tracking/track.
    final trackOk = await client.track(
      'purchase',
      userId,
      experimentKey: experimentKey,
      value: 12.5,
      properties: {'sdk': 'flutter', 'currency': 'USD'},
    );
    if (!trackOk) _fail('track purchase failed');

    // 4. Track `page_view` without a key → POST /tracking/batch fanned out to the
    //    cached assignment + flag.
    if (client.getAssignments(userId).length != 1) {
      _fail('expected one cached assignment before fan-out');
    }
    final fanoutOk = await client.track('page_view', userId, properties: {'page': '/smoke'});
    if (!fanoutOk) _fail('fan-out track failed');

    // The SDK exposes a batch call, so also send a 2-event batch.
    final batchOk = await client.trackBatch([
      TrackEvent(userId: userId, eventName: 'add_to_cart', experimentKey: experimentKey, value: 1),
      TrackEvent(userId: userId, eventName: 'checkout', featureFlagKey: flagKey),
    ]);
    if (!batchOk) _fail('batch track failed');

    stdout.writeln(jsonEncode({
      'sdk': 'flutter',
      'assign': {
        'variant_name': first.variantName,
        'is_control': first.isControl,
        'sticky': sticky,
      },
      'flag': {'enabled': flag.enabled},
      'track': {'ok': trackOk},
      'fanout': {'ok': fanoutOk && batchOk},
    }));
  } finally {
    await client.close();
    await fresh.close();
  }
}
