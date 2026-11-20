/// Unit tests for ExperimentationClient against the backend contract.
///
/// The HTTP layer is mocked with `MockClient` from `package:http/testing.dart`
/// (no code generation required); every request is recorded so URL, method,
/// headers and JSON body can be asserted.
///
/// Run with:
///   flutter test test/client_test.dart
import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:controlvar/controlvar.dart' show SharedPreferencesOfflineStore;
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

/// The server's reading of a recorded URL, as Starlette reads the query: the
/// part after `?` and before `#`, split on `&`, each pair at its first `=`,
/// `+` read as a space, then percent-decoded as UTF-8
/// (`Uri.decodeQueryComponent` does both; `Uri.decodeComponent` leaves `+`).
/// Not `Uri.queryParameters`, so that the order and the number of parameters
/// can be asserted.
List<MapEntry<String, String>> _serverQuery(Uri url) {
  final text = url.toString().split('#').first;
  final at = text.indexOf('?');
  if (at < 0) return [];
  return [
    for (final pair in text.substring(at + 1).split('&'))
      if (pair.isNotEmpty)
        MapEntry(
          Uri.decodeQueryComponent(pair.split('=').first),
          Uri.decodeQueryComponent(pair.contains('=') ? pair.substring(pair.indexOf('=') + 1) : ''),
        ),
  ];
}

/// The `context` parameter of a recorded URL read back as JSON, or `null`
/// when there is none.
Object? _serverContext(Uri url) {
  final values = [
    for (final pair in _serverQuery(url))
      if (pair.key == 'context') pair.value,
  ];
  return values.isEmpty ? null : json.decode(values.first);
}

/// A flag server whose answer depends on the context it decodes: `enabled`
/// only for a user whose attributes say `plan == pro`.
http.Response _ruleServer(http.Request request) {
  final context = _serverContext(request.url);
  final matches = context is Map && context['plan'] == 'pro';
  return _Server._json(200, {'key': 'f', 'enabled': matches, 'config': null});
}

/// Alphanumeric on purpose: a non-ASCII canary would hide in a percent escape.
const _canary = 'attrcanary7f3';
const _evaluateUrl = 'https://api.example.com/api/v1/feature-flags/evaluate/f';

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
  // evaluateFlag sends the user's attributes (#438)
  // -------------------------------------------------------------------------
  group('evaluateFlag sends the attributes', () {
    setUp(() {
      server.respond = _ruleServer;
    });

    test('regression #438: evaluateFlag sends the attributes as context after user_id', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});

      expect(server.requests, hasLength(1));
      expect(server.last.url.toString(),
          '$_evaluateUrl?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D');
    });

    test('regression #438: a targeting rule on attributes matches', () async {
      final client = await _client(server);

      final pro = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      final none = await client.evaluateFlag('f', 'u2');

      expect(pro.enabled, isTrue, reason: 'the rule plan == pro must match from the attributes');
      expect(none.enabled, isFalse);
    });

    test('regression #438: isEnabled passes the attributes on', () async {
      final client = await _client(server);

      expect(await client.isEnabled('f', 'u1', attributes: {'plan': 'pro'}), isTrue);
      expect(server.last.url.toString(),
          '$_evaluateUrl?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D');
    });

    test('no attributes, or an empty map, send the user id alone', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1');
      expect(server.last.url.toString(), '$_evaluateUrl?user_id=u1');
      client.clearCache();
      await client.evaluateFlag('f', 'u1', attributes: {});
      expect(server.last.url.toString(), '$_evaluateUrl?user_id=u1');
      expect(server.requests, hasLength(2));
    });

    test('no attributes and an empty map are one cache slot', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1');
      await client.evaluateFlag('f', 'u1', attributes: {});

      expect(server.requests, hasLength(1));
    });

    test('the context reads back as the attributes with the servers rules', () async {
      final vectors = <Map<String, dynamic>>[
        {'q': "a+b&c=d#e f/é😀!'()*~"},
        {'q': 'a+b&c=d#e fé'},
        {
          'age': 30,
          'beta': true,
          'device': {'os': 'ios', 'version': '17.4.0'},
          'tags': ['a', 'b'],
        },
      ];
      for (final attributes in vectors) {
        server.requests.clear();
        final client = await _client(server);

        await client.evaluateFlag('f', 'u1', attributes: attributes);

        expect(server.requests, hasLength(1));
        final url = server.last.url;
        expect([for (final pair in _serverQuery(url)) pair.key], ['user_id', 'context'],
            reason: 'no other parameter, user_id first: $url');
        expect(_serverQuery(url).first.value, 'u1');
        expect(_serverContext(url), attributes);
      }
    });

    test('exact URL for plus, ampersand, equals, hash, space and accent', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1', attributes: {'q': 'a+b&c=d#e fé'});

      // Written from encodeQueryComponent (everything but letters, digits and
      // -._~ is escaped, a space is +); not measured on a device. The
      // correctness check is the decode test above.
      expect(server.last.url.toString(),
          '$_evaluateUrl?user_id=u1&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e+f%C3%A9%22%7D');
    });

    test('cache: other attributes ask the server again', () async {
      final client = await _client(server);

      final pro = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      final free = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'});

      expect([pro.enabled, free.enabled], [true, false]);
      expect(server.requests, hasLength(2));
    });

    test('cache: attributes that differ by a key or by a type are two requests', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      await client.evaluateFlag('f', 'u1', attributes: {'tier': 'pro'});
      expect(server.requests, hasLength(2));

      await client.evaluateFlag('f', 'u2', attributes: {'n': 1});
      await client.evaluateFlag('f', 'u2', attributes: {'n': '1'});
      expect(server.requests, hasLength(4));
    });

    test('cache: one entry per user and flag, the last answer wins', () async {
      final client = await _client(server);

      final first = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      final other = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'});
      final again = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});

      expect([first.enabled, other.enabled, again.enabled], [true, false, true]);
      expect(server.requests, hasLength(3));
    });

    test('cache: the same attributes twice are one request', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});

      expect(server.requests, hasLength(1));
    });

    test('cache: attributes, then none, are two requests and the second is the answer for none', () async {
      final client = await _client(server);

      final pro = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      final none = await client.evaluateFlag('f', 'u1');

      expect([pro.enabled, none.enabled], [true, false]);
      expect(server.requests, hasLength(2));
    });

    test('cache: the same attributes in another order are one request, written with sorted keys', () async {
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1', attributes: {
        'plan': 'pro',
        'device': {'os': 'ios', 'v': '1'},
      });
      await client.evaluateFlag('f', 'u1', attributes: {
        'device': {'v': '1', 'os': 'ios'},
        'plan': 'pro',
      });

      expect(server.requests, hasLength(1));
      expect(
          server.last.url.toString(),
          '$_evaluateUrl?user_id=u1&context='
          '%7B%22device%22%3A%7B%22os%22%3A%22ios%22%2C%22v%22%3A%221%22%7D%2C%22plan%22%3A%22pro%22%7D');
    });

    test('in flight: concurrent calls with other attributes are two requests, with the same attributes one',
        () async {
      final client = await _client(server);

      final others = await Future.wait([
        client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'}),
        client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'}),
      ]);
      expect(others.map((r) => r.enabled), [true, false]);
      expect(server.requests, hasLength(2));

      server.requests.clear();
      final same = await _client(server);
      final results = await Future.wait([
        same.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'}),
        same.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'}),
      ]);
      expect(results.map((r) => r.enabled), [true, true]);
      expect(server.requests, hasLength(1));
    });

    test('the keyless track fan-out lists the flag once, whatever attributes it was asked with', () async {
      final client = await _client(server);
      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'});
      expect(client.getEvaluatedFlags('u1'), ['f']);
      server.requests.clear();
      server.reply(200, {'success_count': 1, 'failure_count': 0, 'errors': null});

      await client.track('page_view', 'u1');

      expect(server.requests, hasLength(1));
      expect(server.lastEvents, hasLength(1));
      expect(server.lastEvents.single['feature_flag_key'], 'f');
    });

    test('flag evaluation and assignment write the attributes alike', () async {
      final attributes = {
        'plan': 'pro',
        'tags': ['a', 'b'],
        'device': {'os': 'ios'},
      };
      server.respond = (request) =>
          request.method == 'POST' ? _Server._json(200, _assignJson) : _ruleServer(request);
      final client = await _client(server);

      await client.evaluateFlag('f', 'u1', attributes: attributes);
      await client.getAssignment('exp-1', 'u1', attributes: attributes);

      final assigned = (json.decode(server.last.body) as Map)['context'];
      expect(_serverContext(server.requests.first.url), assigned);
    });

    test('the offline store keeps one answer per user and flag, whatever the attributes', () async {
      final store = InMemoryOfflineStore();
      final client = await _client(server, offlineFallback: true, store: store);

      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'});
      expect(store.getFlag('u1', 'f')?.enabled, isFalse, reason: 'the last answer, whatever it was asked with');

      client.clearCache();
      server.fail(Exception('offline'));
      final offline = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      expect(offline.enabled, isFalse, reason: 'served when the server cannot answer, for any attributes');
    });

    test('the persisted key is the user and the flag, as before the attributes', () async {
      TestWidgetsFlutterBinding.ensureInitialized();
      SharedPreferences.setMockInitialValues({});
      final client = await _client(server, offlineFallback: true, store: SharedPreferencesOfflineStore());

      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'});

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getKeys(), {'ep_sdk_flag_2:u1:f'});
      await client.clearOfflineCache();
      expect(prefs.getKeys(), isEmpty);
    });

    test('a 404 under other attributes removes the stored answer and it is not served later', () async {
      final store = InMemoryOfflineStore();
      final client = await _client(server, offlineFallback: true, store: store);
      await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      expect(store.getFlag('u1', 'f')?.enabled, isTrue);
      client.clearCache();

      server.reply(404, {'detail': "Feature flag with key 'f' not found"});
      final gone = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'free'});
      expect(gone.enabled, isFalse);
      expect(store.getFlag('u1', 'f'), isNull);

      server.fail(Exception('offline'));
      final offline = await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});
      expect(offline.enabled, isFalse, reason: 'the stored answer must not come back');
    });

    group('attributes that cannot be written as JSON', () {
      // Nothing is sent, cached or read from the offline store (which holds an
      // enabled answer for the flag), and nothing is printed.
      Future<void> expectNothingSent(Map<String, dynamic> attributes) async {
        final store = InMemoryOfflineStore();
        await store.setFlag('u1', const EvalResult(key: 'f', enabled: true));
        final client = await _client(server, offlineFallback: true, store: store);
        final printed = <String>[];

        final result = await runZoned(
          () => client.evaluateFlag('f', 'u1', attributes: attributes),
          zoneSpecification: ZoneSpecification(print: (self, parent, zone, line) => printed.add(line)),
        );

        expect(result.enabled, isFalse, reason: 'the stored answer must not be served');
        expect(result.key, 'f');
        expect(server.requests, isEmpty);
        expect(client.getEvaluatedFlags('u1'), isEmpty);
        expect(printed.join('\n'), isNot(contains(_canary)));
      }

      test('a DateTime in the attributes: nothing is sent, cached or read from the offline store', () async {
        await expectNothingSent({'when': DateTime(2026, 1, 1), 'note': _canary});
      });

      test('a NaN in the attributes: nothing is sent, cached or read from the offline store', () async {
        await expectNothingSent({'n': double.nan, 'note': _canary});
      });

      test('a cycle in the attributes: nothing is sent, cached or read from the offline store', () async {
        final cycle = <String, dynamic>{'note': _canary};
        cycle['self'] = cycle;
        await expectNothingSent(cycle);
      });

      test('an answer already cached for other attributes is not served either', () async {
        final client = await _client(server);
        await client.evaluateFlag('f', 'u1', attributes: {'plan': 'pro'});

        final result = await client.evaluateFlag('f', 'u1', attributes: {'n': double.nan});

        expect(result.enabled, isFalse);
        expect(server.requests, hasLength(1));
      });
    });
  });

  // -------------------------------------------------------------------------
  // Errors carry no query
  // -------------------------------------------------------------------------
  group('request errors name no query', () {
    final uri = Uri.parse('$_evaluateUrl?user_id=u1&context=$_canary');

    ApiHttpClient api({String baseUrl = 'https://api.example.com'}) => ApiHttpClient(
          apiKey: 'test-key',
          baseUrl: baseUrl,
          timeout: const Duration(seconds: 2),
          httpClient: server.client,
        );

    Future<Object?> failure(Future<Object?> call) =>
        call.then<Object?>((_) => null, onError: (Object error) => error);

    void expectNoQuery(Object? error) {
      expect(error, isA<ApiException>());
      final text = '${(error as ApiException).message} $error';
      expect(error.statusCode, 0);
      expect(text, isNot(contains(_canary)));
      expect(text, isNot(contains('?')));
      expect(text, isNot(contains('context=')));
    }

    test('a ClientException that names the request URI leaves no query in the ApiException', () async {
      server.fail(http.ClientException('Connection closed before full header was received', uri));

      final error = await failure(api().get(uri.path, query: {'user_id': 'u1', 'context': _canary}));

      expectNoQuery(error);
      expect((error as ApiException).message, 'Network error: the request failed');
    });

    test('any other error text is dropped too', () async {
      server.fail(Exception('GET $uri failed'));

      final error = await failure(api().get(uri.path, query: {'user_id': 'u1', 'context': _canary}));

      expectNoQuery(error);
      expect((error as ApiException).message, 'Network error: the request failed');
    });

    test('a timeout is reported as one, without its text', () async {
      server.fail(TimeoutException('GET $uri timed out'));

      final error = await failure(api().get(uri.path, query: {'user_id': 'u1', 'context': _canary}));

      expectNoQuery(error);
      expect((error as ApiException).message, 'Network error: the request timed out');
    });

    test('a POST error is rebuilt the same way', () async {
      server.fail(http.ClientException('failed', uri));

      final error = await failure(api().post('/api/v1/tracking/track', {'note': _canary}));

      expectNoQuery(error);
    });

    test('a URL that does not parse is an ApiException, not the FormatException that names it', () async {
      final error = await failure(
        api(baseUrl: 'https://api.example.com:$_canary')
            .get(uri.path, query: {'user_id': 'u1', 'context': _canary}),
      );

      expectNoQuery(error);
      expect(server.requests, isEmpty);
    });

    test('evaluateFlag returns the disabled answer and prints nothing when the error names the attributes',
        () async {
      server.fail(http.ClientException('failed', uri));
      final client = await _client(server);
      final printed = <String>[];

      final result = await runZoned(
        () => client.evaluateFlag('f', 'u1', attributes: {'note': _canary}),
        zoneSpecification: ZoneSpecification(print: (self, parent, zone, line) => printed.add(line)),
      );

      expect(result.enabled, isFalse);
      expect(printed.join('\n'), isNot(contains(_canary)));
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

    test('a 404 (a deleted flag) never serves a stale value and removes it', () async {
      final store = InMemoryOfflineStore();
      server.reply(200, _flagJson);
      final client = await _client(server, offlineFallback: true, store: store);
      await client.evaluateFlag('new-ui', 'user-1');
      client.clearCache();

      server.reply(404, {'detail': "Feature flag with key 'new-ui' not found"});
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

    test('a context given to get must equal the one stored; none means no check', () {
      final cache = EvaluationCache<int>(ttl: const Duration(minutes: 1));
      cache.set('u', 'k', 1, context: '{"a":1}');

      expect(cache.get('u', 'k', context: '{"a":1}'), 1);
      expect(cache.get('u', 'k', context: '{"a":2}'), isNull);
      expect(cache.get('u', 'k', context: ''), isNull);
      expect(cache.get('u', 'k'), 1, reason: 'assignments and the listings ask without a context');
      expect(cache.entries('u').map((e) => e.key), ['k']);
    });

    test('a set without a context stores the empty one, and a later set replaces it', () {
      final cache = EvaluationCache<int>(ttl: const Duration(minutes: 1));
      cache.set('u', 'k', 1);

      expect(cache.get('u', 'k', context: ''), 1);
      expect(cache.get('u', 'k', context: '{"a":1}'), isNull);
      cache.set('u', 'k', 2, context: '{"a":1}');
      expect(cache.get('u', 'k', context: ''), isNull);
      expect(cache.get('u', 'k', context: '{"a":1}'), 2);
      expect(cache.size, 1);
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
