/// Lightweight HTTP client wrapper around the `http` package.
library controlvar_http_client;

import 'dart:convert';
import 'package:http/http.dart' as http;

/// Wraps the `http` package to provide a simple JSON-oriented API with
/// configurable timeouts and API-key authentication.
///
/// Every request carries `X-API-Key`, `Content-Type: application/json` and
/// `Accept: application/json`.
class ApiHttpClient {
  final String apiKey;
  final String baseUrl;
  final Duration timeout;

  /// Underlying HTTP client; inject `MockClient` from `package:http/testing.dart` in tests.
  final http.Client _client;

  ApiHttpClient({
    required this.apiKey,
    required this.baseUrl,
    required this.timeout,
    http.Client? httpClient,
  }) : _client = httpClient ?? http.Client();

  Map<String, String> get _headers => {
        'X-API-Key': apiKey,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      };

  Uri _uri(String path, [Map<String, String>? query]) {
    final origin = baseUrl.replaceAll(RegExp(r'/+$'), '');
    var url = '$origin$path';
    if (query != null && query.isNotEmpty) {
      final encoded = query.entries
          .map((e) => '${Uri.encodeQueryComponent(e.key)}=${Uri.encodeQueryComponent(e.value)}')
          .join('&');
      url += (url.contains('?') ? '&' : '?') + encoded;
    }
    return Uri.parse(url);
  }

  /// Performs a GET request and decodes the JSON response body.
  ///
  /// [path] must already have its segments percent-encoded
  /// (`Uri.encodeComponent`); [query] values are encoded here.
  /// Throws [ApiException] on non-2xx responses or network errors.
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    final uri = _uri(path, query);
    try {
      final response = await _client.get(uri, headers: _headers).timeout(timeout);
      return _decode(response);
    } on ApiException {
      rethrow;
    } catch (e) {
      throw ApiException(statusCode: 0, message: 'Network error: $e');
    }
  }

  /// Performs a POST request with a JSON body.
  ///
  /// Returns the decoded response body (an empty map for an empty body).
  /// Throws [ApiException] on non-2xx responses or network errors.
  Future<Map<String, dynamic>> post(String path, Map<String, dynamic> body) async {
    final uri = _uri(path);
    try {
      final response =
          await _client.post(uri, headers: _headers, body: json.encode(body)).timeout(timeout);
      return _decode(response);
    } on ApiException {
      rethrow;
    } catch (e) {
      throw ApiException(statusCode: 0, message: 'Network error: $e');
    }
  }

  Map<String, dynamic> _decode(http.Response response) {
    if (response.statusCode >= 200 && response.statusCode < 300) {
      if (response.body.isEmpty) return {};
      final decoded = json.decode(response.body);
      return decoded is Map ? Map<String, dynamic>.from(decoded) : {'data': decoded};
    }
    throw ApiException(
      statusCode: response.statusCode,
      message: 'API error: ${response.statusCode}',
      body: response.body,
    );
  }

  /// Releases the underlying HTTP client resources.
  void close() {
    _client.close();
  }
}

/// Exception thrown by [ApiHttpClient] on non-2xx responses or network errors
/// (`statusCode == 0` for network errors and timeouts).
class ApiException implements Exception {
  final int statusCode;
  final String message;
  final String? body;

  const ApiException({required this.statusCode, required this.message, this.body});

  bool get isNotFound => statusCode == 404;

  @override
  String toString() => 'ApiException($statusCode): $message';
}
