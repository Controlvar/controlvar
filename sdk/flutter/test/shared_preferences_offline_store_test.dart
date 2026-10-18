/// Tests for the SharedPreferences-backed offline store (Flutter only).
///
/// Run with:
///   flutter test test/shared_preferences_offline_store_test.dart
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:controlvar/controlvar.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late SharedPreferencesOfflineStore store;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    store = SharedPreferencesOfflineStore();
    await store.init();
  });

  test('flag evaluation round-trips per user', () async {
    await store.setFlag(
      'user-1',
      const EvalResult(key: 'new-ui', enabled: true, config: {'variant': 'blue'}),
    );

    final loaded = store.getFlag('user-1', 'new-ui');
    expect(loaded?.enabled, isTrue);
    expect(loaded?.configMap, {'variant': 'blue'});
    expect(store.getFlag('user-2', 'new-ui'), isNull);
    expect(store.getFlag('user-1', 'other'), isNull);
  });

  test('assignment round-trips and can be removed', () async {
    await store.setAssignment(
      'user-1',
      const Assignment(
        experimentKey: 'exp-1',
        userId: 'user-1',
        variantId: 'v-1',
        variantName: 'treatment',
        isControl: false,
        configuration: {'cta': 'Buy'},
      ),
    );

    final loaded = store.getAssignment('user-1', 'exp-1');
    expect(loaded?.variantName, 'treatment');
    expect(loaded?.variantId, 'v-1');
    expect(loaded?.configuration, {'cta': 'Buy'});

    await store.removeAssignment('user-1', 'exp-1');
    expect(store.getAssignment('user-1', 'exp-1'), isNull);
  });

  test('user ids containing ":" do not collide', () async {
    await store.setFlag('a:b', const EvalResult(key: 'c', enabled: true));
    expect(store.getFlag('a', 'b:c'), isNull);
    expect(store.getFlag('a:b', 'c')?.enabled, isTrue);
  });

  test('clear() removes only SDK keys', () async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('app_setting', 'keep');
    await store.setFlag('u', const EvalResult(key: 'f', enabled: true));
    await store.setAssignment(
      'u',
      const Assignment(experimentKey: 'e', userId: 'u', variantName: 'control', isControl: true),
    );

    await store.clear();

    expect(store.getFlag('u', 'f'), isNull);
    expect(store.getAssignment('u', 'e'), isNull);
    expect(prefs.getString('app_setting'), 'keep');
  });

  test('works end-to-end with the client on network failure', () async {
    final client = ExperimentationClient(
      config: const SdkConfig(apiKey: 'k', baseUrl: 'http://127.0.0.1:9', offlineFallback: true),
      offlineStore: store,
    );
    await client.init();
    await store.setFlag('u', const EvalResult(key: 'f', enabled: true));

    final result = await client.evaluateFlag('f', 'u');
    expect(result.enabled, isTrue, reason: 'unreachable API → persisted value');
    await client.close();
  });
}
