/// Flutter-free entry point of the Controlvar SDK.
///
/// Exports everything except the SharedPreferences offline store, so it can
/// be imported from plain Dart programs (server-side Dart, CLI tools, the
/// contract smoke) where `package:flutter` is not available.
///
/// ```dart
/// import 'package:controlvar/controlvar_core.dart';
/// ```
library controlvar_core;

export 'src/cache.dart';
export 'src/client.dart';
export 'src/evaluator.dart' show hashUser;
export 'src/http_client.dart';
export 'src/models.dart';
export 'src/offline_store.dart';
