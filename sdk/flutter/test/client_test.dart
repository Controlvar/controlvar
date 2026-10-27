/// Unit tests for ExperimentationClient against the backend contract.
///
/// The HTTP layer is mocked with `MockClient` from `package:http/testing.dart`
/// (no code generation required); every request is recorded so URL, method,
/// headers and JSON body can be asserted.
///
/// Run with:
///   flutter test test/client_test.dart
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:controlvar/controlvar_core.dart';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const _assignJson = {
  'experiment_key': 'exp-1',
  'user_id': 'user-1',
  'variant_id': '11111111-2222-3333-4444-555555555555',
  'variant_name': 'treatment',
  'is_control': false,
  'configuration': {'headline': 'Go', 'limit': 3},
};

const _flagJson = {
  'key': 'new-ui',
  'enabled': true,
  'config': {'variant': 'blue', 'limit': 5},
};

/// Records requests and answers with [respond] (or throws [error]).
class _Server {
  final List<http.Request> requests = [];
  http.Response Function(http.Request request) respond;
  Object? error;

  _Server({http.Response Function(http.Request)? respond})
      : respond = respond ?? ((_) => _json(200, {}));

  static http.Response _json(int status, Object body) => http.Response(
        body is String ? body : json.encode(body),
        status,
        headers: {'content-type': 'application/json'},
      );

  void reply(int status, Object body) {
    error = null;
    respond = (_) => _json(status, body);
  }

  void fail(Object err) => error = err;

  http.Client get client => MockClient((request) async {
        requests.add(request);
        if (error != null) throw error!;
        return respond(request);
      });

  http.Request get last => requests.last;
  Map<String, dynamic> get lastBody => Map<String, dynamic>.from(json.decode(last.body) as Map);
  List<Map<String, dynamic>> get lastEvents =>
      (lastBody['events'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
}

SdkConfig _config({
  bool offlineFallback = false,
  Duration cacheTtl = const Duration(minutes: 5),
}) =>
    SdkConfig(
      apiKey: 'test-key',
      baseUrl: 'https://api.example.com/',
      cacheTtl: cacheTtl,
      offlineFallback: offlineFallback,
    );

Future<ExperimentationClient> _client(
  _Server server, {
  bool offlineFallback = false,
  Duration cacheTtl = const Duration(minutes: 5),
  OfflineStore? store,
  bool init = true,
}) async {
  final client = ExperimentationClient(
    config: _config(offlineFallback: offlineFallback, cacheTtl: cacheTtl),
    httpClient: ApiHttpClient(
      apiKey: 'test-key',
      baseUrl: 'https://api.example.com/',
      timeout: const Duration(seconds: 2),
      httpClient: server.client,
    ),
    offlineStore: store,
  );
  if (init) await client.init();
  return client;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

void main() {
  late _Server server;

  setUp(() {
    server = _Server();
  });

  // -------------------------------------------------------------------------
  // Initialisation
  // -------------------------------------------------------------------------
  group('initialisation', () {
    test('init() completes, is idempotent and makes no request', () async {
      final client = await _client(server);
      await client.init();
      expect(server.requests, isEmpty);
    });

    test('calling evaluateFlag before init() throws StateError', () async {
      final client = await _client(server, init: false);
      expect(() => client.evaluateFlag('flag', 'user-1'), throwsA(isA<StateError>()));
    });

    test('calling track before init() throws StateError', () async {
      final client = await _client(server, init: false);
      expect(() => client.track('e', 'user-1'), throwsA(isA<StateError>()));
    });
  });

  // -------------------------------------------------------------------------
  // evaluateFlag
  // -------------------------------------------------------------------------
  group('evaluateFlag', () {
    test('sends GET /api/v1/feature-flags/evaluate/{key}?user_id= with headers', () async {
      server.reply(200, _flagJson);
      final client = await _client(server);

      await client.evaluateFlag('new ui/v2', 'user a&b');

      expect(server.requests, hasLength(1));
      final request = server.last;
      expect(request.method, 'GET');
      expect(request.url.toString(),
          'https://api.example.com/api/v1/feature-flags/evaluate/new%20ui%2Fv2?user_id=user+a%26b');
      expect(request.url.queryParameters['user_id'], 'user a&b');
      expect(request.headers['X-API-Key'], 'test-key');
      expect(request.headers['Accept'], 'application/json');
      expect(request.headers['Content-Type'], startsWith('application/json'));
    });

    test('maps key, enabled and config', () async {
      server.reply(200, _flagJson);
      final client = await _client(server);

      final result = await client.evaluateFlag('new-ui', 'user-1');

      expect(result.key, 'new-ui');
      expect(result.enabled, isTrue);
      expect(result.configMap?['variant'], 'blue');
      expect(result.configMap?['limit'], 5);
      expect(await client.isEnabled('new-ui', 'user-1'), isTrue);
    });

    test('disabled flag with null config', () async {
      server.reply(200, {'key': 'off', 'enabled': false, 'config': null});
      final client = await _client(server);

      final result = await client.evaluateFlag('off', 'user-1');

      expect(result.enabled, isFalse);
      expect(result.config, isNull);
      expect(result.configMap, isNull);
    });

    test('scalar config is exposed as-is', () async {
      server.reply(200, {'key': 'k', 'enabled': true, 'config': 'blue'});
      final client = await _client(server);

      final result = await client.evaluateFlag('k', 'user-1');
      expect(result.config, 'blue');
      expect(result.configMap, isNull);
    });

    test('second call is served from cache — one HTTP request', () async {
      server.reply(200, _flagJson);
      final client = await _client(server);

      await client.evaluateFlag('new-ui', 'user-1');
      server.reply(500, 'gone');
      final result = await client.evaluateFlag('new-ui', 'user-1');

      expect(result.enabled, isTrue);
      expect(server.requests, hasLength(1));
      expect(client.getEvaluatedFlags('user-1'), ['new-ui']);
    });

    test('cache is per (userId, flagKey) pair', () async {
      server.reply(200, _flagJson);
      final client = await _client(server);

      await client.evaluateFlag('new-ui', 'user-A');
      await client.evaluateFlag('new-ui', 'user-B');

      expect(server.requests, hasLength(2));
      expect(client.getEvaluatedFlags('user-B'), ['new-ui']);
    });

    test('cache entry expires after the TTL', () async {
      server.reply(200, _flagJson);
      final client = await _client(server, cacheTtl: const Duration(milliseconds: 40));

      await client.evaluateFlag('new-ui', 'user-1');
      await Future<void>.delayed(const Duration(milliseconds: 100));
      server.reply(200, {'key': 'new-ui', 'enabled': false, 'config': null});
      final result = await client.evaluateFlag('new-ui', 'user-1');

      expect(result.enabled, isFalse, reason: 'after TTL expiry the flag is re-fetched');
      expect(server.requests, hasLength(2));
    });

    test('clearCache causes next call to re-fetch', () async {
      server.reply(200, _flagJson);
      final client = await _client(server);

      await client.evaluateFlag('new-ui', 'user-1');
      client.clearCache();
      await client.evaluateFlag('new-ui', 'user-1');

      expect(server.requests, hasLength(2));
    });

    test('concurrent calls for the same user + key share one request', () async {
      server.respond = (_) {
        return http.Response(json.encode(_flagJson), 200);
      };
      final client = await _client(server);

      final results = await Future.wait([
        client.evaluateFlag('new-ui', 'user-1'),
        client.evaluateFlag('new-ui', 'user-1'),
        client.evaluateFlag('new-ui', 'user-1'),
      ]);

      expect(results.map((r) => r.enabled), everyElement(isTrue));
      expect(server.requests, hasLength(1));
    });

    test('404 → disabled, not cached, never throws', () async {
      server.reply(404, {'detail': "Feature flag with key 'ghost' not found"});
      final client = await _client(server);

      final result = await client.evaluateFlag('ghost', 'user-1');
      expect(result.enabled, isFalse);
      expect(result.key, 'ghost');
      expect(client.getEvaluatedFlags('user-1'), isEmpty);

      await client.evaluateFlag('ghost', 'user-1');
      expect(server.requests, hasLength(2), reason: 'failures are never cached');
    });

    test('500 → disabled when nothing cached', () async {
      server.reply(500, {'detail': 'boom'});
      final client = await _client(server);
      expect((await client.evaluateFlag('flag', 'user-1')).enabled, isFalse);
    });

    test('401 → disabled', () async {
      server.reply(401, {'detail': 'Unauthorized'});
      final client = await _client(server);
      expect((await client.evaluateFlag('flag', 'user-1')).enabled, isFalse);
    });

    test('network error → disabled, never throws', () async {
      server.fail(Exception('Connection refused'));
      final client = await _client(server);
      expect((await client.evaluateFlag('flag', 'user-1')).enabled, isFalse);
    });

    test('malformed body → disabled', () async {
      server.reply(200, 'not json');
      final client = await _client(server);
      expect((await client.evaluateFlag('flag', 'user-1')).enabled, isFalse);
    });
  });

  // -------------------------------------------------------------------------
  // Offline fallback
  // -------------------------------------------------------------------------
  group('offline fallback', () {
    test('stores the evaluation and serves it when the network fails', () async {
      final store = InMemoryOfflineStore();
      server.reply(200, _flagJson);
      final client = await _client(server, offlineFallback: true, store: store);

      await client.evaluateFlag('new-ui', 'user-1');
      expect(store.getFlag('user-1', 'new-ui')?.enabled, isTrue);

      client.clearCache();
      server.fail(Exception('offline'));
      final result = await client.evaluateFlag('new-ui', 'user-1');
      expect(result.enabled, isTrue, reason: 'last known value served offline');
    });

    test('a 404 never serves a stale value and removes it', () async {
      final store = InMemoryOfflineStore();
      server.reply(200, _flagJson);
      final client = await _client(server, offlineFallback: true, store: store);
      await client.evaluateFlag('new-ui', 'user-1');
      client.clearCache();

      server.reply(404, {'detail': 'archived'});
      final result = await client.evaluateFlag('new-ui', 'user-1');

      expect(result.enabled, isFalse);
      expect(store.getFlag('user-1', 'new-ui'), isNull);
    });

    test('offlineFallback=false never touches the store', () async {
      final store = InMemoryOfflineStore();
      server.reply(200, _flagJson);
      final client = await _client(server, offlineFallback: false, store: store);
      await client.evaluateFlag('new-ui', 'user-1');
      expect(store.getFlag('user-1', 'new-ui'), isNull);
    });

    test('assignment is persisted and served offline', () async {
      final store = InMemoryOfflineStore();
      server.reply(200, _assignJson);
      final client = await _client(server, offlineFallback: true, store: store);
      await client.getAssignment('exp-1', 'user-1');
      client.clearCache();

      server.fail(Exception('offline'));
      final assignment = await client.getAssignment('exp-1', 'user-1');
      expect(assignment?.variantName, 'treatment');
    });
  });

  // -------------------------------------------------------------------------
  // getAssignment
  // -------------------------------------------------------------------------
  group('getAssignment', () {
    test('POSTs experiment_key, user_id and context to /api/v1/tracking/assign', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);

      await client.getAssignment('exp-1', 'user-1', attributes: {'plan': 'pro', 'beta': true});

      expect(server.requests, hasLength(1));
      final request = server.last;
      expect(request.method, 'POST');
      expect(request.url.toString(), 'https://api.example.com/api/v1/tracking/assign');
      expect(request.headers['X-API-Key'], 'test-key');
      expect(request.headers['Content-Type'], startsWith('application/json'));
      expect(server.lastBody, {
        'experiment_key': 'exp-1',
        'user_id': 'user-1',
        'context': {'plan': 'pro', 'beta': true},
      });
    });

    test('omits context without attributes', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);

      await client.getAssignment('exp-1', 'user-1');

      expect(server.lastBody, {'experiment_key': 'exp-1', 'user_id': 'user-1'});
    });

    test('maps the response', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);

      final assignment = await client.getAssignment('exp-1', 'user-1');

      expect(assignment, isNotNull);
      expect(assignment!.experimentKey, 'exp-1');
      expect(assignment.userId, 'user-1');
      expect(assignment.variantId, '11111111-2222-3333-4444-555555555555');
      expect(assignment.variantName, 'treatment');
      expect(assignment.isControl, isFalse);
      expect(assignment.configuration, {'headline': 'Go', 'limit': 3});
      expect(await client.getVariant('exp-1', 'user-1'), 'treatment');
    });

    test('control with null configuration', () async {
      server.reply(200, {
        'experiment_key': 'exp-1',
        'user_id': 'user-1',
        'variant_id': 'v',
        'variant_name': 'control',
        'is_control': true,
        'configuration': null,
      });
      final client = await _client(server);

      final assignment = await client.getAssignment('exp-1', 'user-1');
      expect(assignment?.isControl, isTrue);
      expect(assignment?.configuration, isNull);
    });

    test('is sticky from cache — one HTTP request', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);

      final first = await client.getAssignment('exp-1', 'user-1');
      server.reply(500, 'gone');
      final second = await client.getAssignment('exp-1', 'user-1');

      expect(second?.variantName, first?.variantName);
      expect(server.requests, hasLength(1));
      expect(client.getAssignments('user-1').map((a) => a.experimentKey), ['exp-1']);
    });

    test('404 → null, never throws, not cached', () async {
      server.reply(404, {'detail': "No experiment has the key 'ghost'."});
      final client = await _client(server);

      expect(await client.getAssignment('ghost', 'user-1'), isNull);
      expect(client.getAssignments('user-1'), isEmpty);
      await client.getAssignment('ghost', 'user-1');
      expect(server.requests, hasLength(2));
    });

    test('500 → null when nothing cached', () async {
      server.reply(500, {'detail': 'Error'});
      final client = await _client(server);
      expect(await client.getAssignment('exp-1', 'user-1'), isNull);
    });

    test('network error → null when nothing cached', () async {
      server.fail(Exception('Connection refused'));
      final client = await _client(server);
      expect(await client.getAssignment('exp-1', 'user-1'), isNull);
    });

    test('concurrent calls share one request', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);

      await Future.wait([
        client.getAssignment('exp-1', 'user-1'),
        client.getAssignment('exp-1', 'user-1'),
      ]);

      expect(server.requests, hasLength(1));
    });
  });

  // -------------------------------------------------------------------------
  // track with a key
  // -------------------------------------------------------------------------
  group('track with a key', () {
    test('POSTs the event to /api/v1/tracking/track', () async {
      server.reply(200, {'id': 'evt-1'});
      final client = await _client(server);

      final ok = await client.track(
        'purchase',
        'user-1',
        experimentKey: 'exp-1',
        value: 12.5,
        properties: {'sku': 'pro', 'qty': 2},
        timestamp: DateTime.utc(2023, 11, 14, 22, 13, 20),
      );

      expect(ok, isTrue);
      expect(server.requests, hasLength(1));
      expect(server.last.method, 'POST');
      expect(server.last.url.toString(), 'https://api.example.com/api/v1/tracking/track');
      expect(server.last.headers['X-API-Key'], 'test-key');
      expect(server.lastBody, {
        'event_type': 'purchase',
        'event_name': 'purchase',
        'user_id': 'user-1',
        'experiment_key': 'exp-1',
        'value': 12.5,
        'metadata': {'sku': 'pro', 'qty': 2},
        'timestamp': '2023-11-14T22:13:20.000Z',
      });
    });

    test('feature flag key and custom event type', () async {
      server.reply(200, {});
      final client = await _client(server);

      await client.track('search', 'user-1', featureFlagKey: 'new-search', eventType: 'interaction');

      expect(server.lastBody, {
        'event_type': 'interaction',
        'event_name': 'search',
        'user_id': 'user-1',
        'feature_flag_key': 'new-search',
      });
    });

    test('trackEvent accepts a prebuilt TrackEvent', () async {
      server.reply(200, {});
      final client = await _client(server);

      final ok = await client.trackEvent(
        const TrackEvent(userId: 'user-1', eventName: 'signup', experimentKey: 'exp-1'),
      );

      expect(ok, isTrue);
      expect(server.lastBody['experiment_key'], 'exp-1');
    });
  });

  // -------------------------------------------------------------------------
  // track without a key (fan-out)
  // -------------------------------------------------------------------------
  group('track without a key', () {
    test('nothing cached → no request, returns true', () async {
      server.reply(200, {});
      final client = await _client(server);

      final ok = await client.track('page_view', 'user-1');

      expect(ok, isTrue);
      expect(server.requests, isEmpty);
    });

    test('fans out to cached assignments and flags of that user via /tracking/batch', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);
      await client.getAssignment('exp-1', 'user-1');
      server.reply(200, _flagJson);
      await client.evaluateFlag('new-ui', 'user-1');
      await client.evaluateFlag('new-ui', 'someone-else');
      server.requests.clear();
      server.reply(200, {'success_count': 2, 'failure_count': 0, 'errors': null});

      final ok = await client.track('page_view', 'user-1', properties: {'page': '/'}, value: 1);

      expect(ok, isTrue);
      expect(server.requests, hasLength(1));
      expect(server.last.method, 'POST');
      expect(server.last.url.toString(), 'https://api.example.com/api/v1/tracking/batch');
      final events = server.lastEvents;
      expect(events, hasLength(2), reason: 'one per cached assignment + one per cached flag');
      expect(events[0]['experiment_key'], 'exp-1');
      expect(events[0].containsKey('feature_flag_key'), isFalse);
      expect(events[1]['feature_flag_key'], 'new-ui');
      expect(events[1].containsKey('experiment_key'), isFalse);
      for (final event in events) {
        expect(event['event_type'], 'page_view');
        expect(event['event_name'], 'page_view');
        expect(event['user_id'], 'user-1');
        expect(event['value'], 1);
        expect(event['metadata'], {'page': '/'});
      }
    });

    test('expired cache entries are not fanned out', () async {
      server.reply(200, _flagJson);
      final client = await _client(server, cacheTtl: const Duration(milliseconds: 40));
      await client.evaluateFlag('new-ui', 'user-1');
      await Future<void>.delayed(const Duration(milliseconds: 100));
      server.requests.clear();

      await client.track('page_view', 'user-1');

      expect(server.requests, isEmpty);
    });
  });

  // -------------------------------------------------------------------------
  // track never throws
  // -------------------------------------------------------------------------
  group('track never throws', () {
    test('API error → false', () async {
      server.reply(500, {'detail': 'Error'});
      final client = await _client(server);
      expect(await client.track('e', 'user-1', experimentKey: 'x'), isFalse);
    });

    test('422 → false', () async {
      server.reply(422, {'detail': 'invalid'});
      final client = await _client(server);
      expect(await client.track('e', 'user-1', featureFlagKey: 'f'), isFalse);
    });

    test('network error → false', () async {
      server.fail(Exception('Connection refused'));
      final client = await _client(server);
      await expectLater(client.track('e', 'user-1', experimentKey: 'x'), completion(isFalse));
    });
  });

  // -------------------------------------------------------------------------
  // trackBatch
  // -------------------------------------------------------------------------
  group('trackBatch', () {
    test('sends one /tracking/batch request', () async {
      server.reply(200, {'success_count': 2, 'failure_count': 0, 'errors': null});
      final client = await _client(server);

      final ok = await client.trackBatch([
        const TrackEvent(userId: 'u', eventName: 'add_to_cart', experimentKey: 'exp-1', value: 1),
        const TrackEvent(userId: 'u', eventName: 'checkout', featureFlagKey: 'new-ui'),
      ]);

      expect(ok, isTrue);
      expect(server.requests, hasLength(1));
      expect(server.last.url.path, '/api/v1/tracking/batch');
      final events = server.lastEvents;
      expect(events, hasLength(2));
      expect(events[0]['experiment_key'], 'exp-1');
      expect(events[1]['feature_flag_key'], 'new-ui');
    });

    test('empty list and unkeyed events without cache send nothing', () async {
      final client = await _client(server);

      expect(await client.trackBatch([]), isTrue);
      expect(await client.trackBatch([const TrackEvent(userId: 'u', eventName: 'page_view')]), isTrue);
      expect(server.requests, isEmpty);
    });

    test('fans out unkeyed events and chunks by 100', () async {
      server.reply(200, _assignJson);
      final client = await _client(server);
      await client.getAssignment('exp-1', 'u');
      server.requests.clear();
      server.reply(200, {});

      final events = [
        for (var i = 0; i < 149; i++) TrackEvent(userId: 'u', eventName: 'e$i', experimentKey: 'exp-1'),
        const TrackEvent(userId: 'u', eventName: 'page_view'), // fans out to exp-1 → 150
      ];
      final ok = await client.trackBatch(events);

      expect(ok, isTrue);
      expect(server.requests, hasLength(2));
      expect((json.decode(server.requests[0].body)['events'] as List), hasLength(100));
      final second = json.decode(server.requests[1].body)['events'] as List;
      expect(second, hasLength(50));
      expect(second.last['event_name'], 'page_view');
      expect(second.last['experiment_key'], 'exp-1');
    });

    test('reports failure', () async {
      server.reply(429, {'detail': 'rate limited'});
      final client = await _client(server);
      expect(
        await client.trackBatch([const TrackEvent(userId: 'u', eventName: 'e', experimentKey: 'x')]),
        isFalse,
      );
    });
  });

  // -------------------------------------------------------------------------
  // close / cache helpers
  // -------------------------------------------------------------------------
  group('close', () {
    test('close() clears caches and closes the HTTP client', () async {
      server.reply(200, _flagJson);
      final client = await _client(server);
      await client.evaluateFlag('new-ui', 'user-1');

      await client.close();

      expect(client.getEvaluatedFlags('user-1'), isEmpty);
    });
  });

  // -------------------------------------------------------------------------
  // EvaluationCache
  // -------------------------------------------------------------------------
  group('EvaluationCache', () {
    test('set/get per user, entries in insertion order, size', () {
      final cache = EvaluationCache<int>(ttl: const Duration(minutes: 1));
      cache.set('u1', 'a', 1);
      cache.set('u1', 'b', 2);
      cache.set('u2', 'a', 3);
      cache.set('u1', 'a', 4);

      expect(cache.get('u1', 'a'), 4);
      expect(cache.get('u2', 'a'), 3);
      expect(cache.get('u2', 'b'), isNull);
      expect(cache.entries('u1').map((e) => e.key), ['a', 'b']);
      expect(cache.size, 3);
      cache.invalidate('u1', 'a');
      expect(cache.containsKey('u1', 'a'), isFalse);
      cache.clear();
      expect(cache.size, 0);
    });

    test('entries expire', () async {
      final cache = EvaluationCache<int>(ttl: const Duration(milliseconds: 20));
      cache.set('u', 'k', 1);
      await Future<void>.delayed(const Duration(milliseconds: 60));
      expect(cache.get('u', 'k'), isNull);
      expect(cache.entries('u'), isEmpty);
    });
  });

  // -------------------------------------------------------------------------
  // Models
  // -------------------------------------------------------------------------
  group('models', () {
    test('Assignment.fromJson / toJson', () {
      final assignment = Assignment.fromJson(_assignJson);
      expect(assignment.variantName, 'treatment');
      expect(assignment.isControl, isFalse);
      expect(assignment.configuration?['limit'], 3);
      // ignore: deprecated_member_use_from_same_package
      expect(assignment.variantKey, 'treatment');
      expect(Assignment.fromJson(assignment.toJson()).variantId, assignment.variantId);
    });

    test('Assignment.fromJson falls back to the requested keys', () {
      final assignment = Assignment.fromJson(
        {'variant_name': 'control', 'is_control': true},
        fallbackExperimentKey: 'exp',
        fallbackUserId: 'u',
      );
      expect(assignment.experimentKey, 'exp');
      expect(assignment.userId, 'u');
      expect(assignment.variantId, isNull);
    });

    test('EvalResult.fromJson / toJson', () {
      final result = EvalResult.fromJson(_flagJson);
      expect(result.key, 'new-ui');
      expect(result.enabled, isTrue);
      expect(EvalResult.fromJson(result.toJson()).configMap, {'variant': 'blue', 'limit': 5});
      expect(EvalResult.fromJson({'enabled': 1}, fallbackKey: 'k').enabled, isFalse);
    });

    test('TrackEvent.toJson wire format', () {
      const event = TrackEvent(userId: 'u', eventName: 'e');
      expect(event.toJson(), {'event_type': 'e', 'event_name': 'e', 'user_id': 'u'});
      expect(event.hasKey, isFalse);
      final attributed = event.attributed(featureFlagKey: 'f');
      expect(attributed.hasKey, isTrue);
      expect(attributed.toJson()['feature_flag_key'], 'f');
      expect(attributed.toJson().containsKey('experiment_key'), isFalse);
    });

    test('TrackEvent treats empty keys as absent', () {
      const event = TrackEvent(userId: 'u', eventName: 'e', experimentKey: '', featureFlagKey: '');
      expect(event.hasKey, isFalse);
      expect(event.toJson().containsKey('experiment_key'), isFalse);
      expect(event.toJson().containsKey('feature_flag_key'), isFalse);
    });
  });
}
