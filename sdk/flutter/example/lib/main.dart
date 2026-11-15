/// Example Flutter application demonstrating the Controlvar SDK.
///
/// This app shows:
///  - SDK initialisation (no network call; flags are decided per user by the server)
///  - Feature flag evaluation (`GET /api/v1/feature-flags/evaluate/{key}`)
///  - Experiment assignment (`POST /api/v1/tracking/assign`, sticky)
///  - Event tracking (never throws; fans out to cached assignments and flags)
///  - Offline fallback (SharedPreferences)
import 'package:flutter/material.dart';
import 'package:controlvar/controlvar.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final client = ExperimentationClient(
    config: const SdkConfig(
      apiKey: 'YOUR_API_KEY_HERE',
      baseUrl: 'http://localhost:8000',
      cacheTtl: Duration(minutes: 5),
      offlineFallback: true,
    ),
    offlineStore: SharedPreferencesOfflineStore(),
  );

  await client.init();

  runApp(ExperimentationApp(client: client));
}

class ExperimentationApp extends StatelessWidget {
  final ExperimentationClient client;

  const ExperimentationApp({super.key, required this.client});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Experimentation SDK Demo',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.blue),
        useMaterial3: true,
      ),
      home: HomePage(client: client),
    );
  }
}

class HomePage extends StatefulWidget {
  final ExperimentationClient client;

  const HomePage({super.key, required this.client});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  static const _userId = 'demo-user-123';

  bool _darkModeEnabled = false;
  String? _checkoutVariant;
  String _status = 'Tap a button to evaluate a flag or get an assignment.';
  bool _loading = false;

  Future<void> _evaluateFlag() async {
    setState(() {
      _loading = true;
      _status = 'Evaluating flag…';
    });

    // evaluateFlag never throws: on failure it returns the cached/offline
    // value or a disabled result.
    final result = await widget.client.evaluateFlag('dark-mode', _userId);
    setState(() {
      _darkModeEnabled = result.enabled;
      _status = 'dark-mode flag: ${result.enabled ? "ENABLED" : "DISABLED"}'
          ' (config: ${result.config})';
      _loading = false;
    });
  }

  Future<void> _getAssignment() async {
    setState(() {
      _loading = true;
      _status = 'Fetching experiment assignment…';
    });

    // getAssignment never throws: `null` means no experiment has the key
    // or the API was unreachable with nothing cached.
    final assignment = await widget.client.getAssignment(
      'checkout-experiment',
      _userId,
      attributes: {'platform': 'flutter', 'plan': 'pro'},
    );
    setState(() {
      _checkoutVariant = assignment?.variantName;
      _status = assignment == null
          ? 'checkout-experiment: not assigned'
          : 'checkout-experiment variant: ${assignment.variantName}'
              ' (control: ${assignment.isControl}, configuration: ${assignment.configuration})';
      _loading = false;
    });
  }

  Future<void> _trackEvent() async {
    // No experiment/flag key: the event is fanned out to every experiment the
    // user has been assigned to and every flag evaluated for them in this client.
    final delivered = await widget.client.track(
      'demo_button_clicked',
      _userId,
      properties: {'source': 'flutter-example', 'screen': 'home'},
    );
    setState(() => _status = delivered
        ? 'Event "demo_button_clicked" tracked.'
        : 'Event "demo_button_clicked" could not be delivered (tracking never throws).');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        backgroundColor: Theme.of(context).colorScheme.inversePrimary,
        title: const Text('Experimentation SDK'),
      ),
      body: Padding(
        padding: const EdgeInsets.all(24.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // Status card
            Card(
              color: Colors.blue.shade50,
              child: Padding(
                padding: const EdgeInsets.all(16.0),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Status',
                      style: Theme.of(context).textTheme.titleSmall?.copyWith(
                            fontWeight: FontWeight.bold,
                          ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      _status,
                      style: Theme.of(context).textTheme.bodyMedium,
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 24),

            // State display
            ListTile(
              title: const Text('dark-mode enabled'),
              trailing: Icon(
                _darkModeEnabled ? Icons.check_circle : Icons.cancel,
                color: _darkModeEnabled ? Colors.green : Colors.red,
              ),
            ),
            ListTile(
              title: const Text('checkout-experiment variant'),
              trailing: Text(
                _checkoutVariant ?? 'none',
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      fontWeight: FontWeight.bold,
                    ),
              ),
            ),
            const SizedBox(height: 24),

            // Action buttons
            FilledButton(
              onPressed: _loading ? null : _evaluateFlag,
              child: const Text('Evaluate "dark-mode" Flag'),
            ),
            const SizedBox(height: 12),
            FilledButton.tonal(
              onPressed: _loading ? null : _getAssignment,
              child: const Text('Get "checkout-experiment" Assignment'),
            ),
            const SizedBox(height: 12),
            OutlinedButton(
              onPressed: _loading ? null : _trackEvent,
              child: const Text('Track Event (fire-and-forget)'),
            ),

            if (_loading) ...[
              const SizedBox(height: 24),
              const Center(child: CircularProgressIndicator()),
            ],
          ],
        ),
      ),
    );
  }
}
