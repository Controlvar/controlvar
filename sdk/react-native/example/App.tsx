/**
 * Example React Native app demonstrating the Controlvar SDK.
 *
 * Shows:
 *  - Provider setup
 *  - useFlag hook for feature flag evaluation
 *  - useExperiment hook for A/B assignment
 *  - useExperimentationClient hook for imperative tracking
 */

import React from 'react';
import {
  ActivityIndicator,
  Button,
  SafeAreaView,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import {
  ExperimentationProvider,
  ExperimentationClient,
  useFlag,
  useExperiment,
  useExperimentationClient,
} from '@controlvar/react-native-sdk';

// ---------------------------------------------------------------------------
// SDK client setup — do this once at app startup.
// ---------------------------------------------------------------------------

const client = new ExperimentationClient({
  apiKey: 'YOUR_API_KEY',
  baseUrl: 'https://api.controlvar.example.com',
  cacheTtlMs: 300_000,
  offlineFallback: true,
});

// ---------------------------------------------------------------------------
// Component: DarkModeStatus — demonstrates useFlag
// ---------------------------------------------------------------------------

function DarkModeStatus(): React.ReactElement {
  const { value, loading, error } = useFlag('dark-mode');

  if (loading) return <ActivityIndicator style={styles.item} />;
  if (error) return <Text style={styles.error}>Flag error: {error.message}</Text>;

  return (
    <View style={styles.row}>
      <Text style={styles.label}>dark-mode flag:</Text>
      <Text style={[styles.value, value ? styles.enabled : styles.disabled]}>
        {value ? 'ENABLED' : 'DISABLED'}
      </Text>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Component: CheckoutExperiment — demonstrates useExperiment
// ---------------------------------------------------------------------------

function CheckoutExperiment(): React.ReactElement {
  const { variant, loading, error } = useExperiment('checkout-experiment');

  if (loading) return <ActivityIndicator style={styles.item} />;
  if (error) return <Text style={styles.error}>Experiment error: {error.message}</Text>;

  return (
    <View style={styles.row}>
      <Text style={styles.label}>checkout-experiment variant:</Text>
      <Text style={styles.value}>{variant ?? 'not assigned'}</Text>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Component: TrackButton — demonstrates useExperimentationClient
// ---------------------------------------------------------------------------

function TrackButton(): React.ReactElement {
  const { client: sdkClient, userId } = useExperimentationClient();
  const [tracked, setTracked] = React.useState(false);

  const handlePress = async () => {
    await sdkClient.track('cta_clicked', userId, {
      source: 'example-app',
      screen: 'home',
    });
    setTracked(true);
  };

  return (
    <View style={styles.item}>
      <Button title="Track Event" onPress={handlePress} />
      {tracked && <Text style={styles.success}>Event tracked!</Text>}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Root App
// ---------------------------------------------------------------------------

export default function App(): React.ReactElement {
  return (
    <ExperimentationProvider
      client={client}
      userId="demo-user-123"
      attributes={{ platform: 'react-native', tier: 'pro' }}
    >
      <SafeAreaView style={styles.container}>
        <Text style={styles.title}>Experimentation SDK Demo</Text>
        <Text style={styles.subtitle}>React Native</Text>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Feature Flags</Text>
          <DarkModeStatus />
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Experiments</Text>
          <CheckoutExperiment />
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Event Tracking</Text>
          <TrackButton />
        </View>
      </SafeAreaView>
    </ExperimentationProvider>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: 24,
    backgroundColor: '#f9fafb',
  },
  title: {
    fontSize: 24,
    fontWeight: '700',
    color: '#111827',
    marginBottom: 4,
  },
  subtitle: {
    fontSize: 14,
    color: '#6b7280',
    marginBottom: 32,
  },
  section: {
    marginBottom: 24,
    backgroundColor: '#ffffff',
    borderRadius: 12,
    padding: 16,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.05,
    shadowRadius: 2,
    elevation: 1,
  },
  sectionTitle: {
    fontSize: 12,
    fontWeight: '600',
    color: '#9ca3af',
    textTransform: 'uppercase',
    letterSpacing: 1,
    marginBottom: 12,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  label: {
    fontSize: 15,
    color: '#374151',
  },
  value: {
    fontSize: 15,
    fontWeight: '600',
    color: '#374151',
  },
  enabled: {
    color: '#16a34a',
  },
  disabled: {
    color: '#ef4444',
  },
  item: {
    marginVertical: 8,
  },
  error: {
    color: '#ef4444',
    fontSize: 13,
  },
  success: {
    color: '#16a34a',
    fontSize: 13,
    marginTop: 8,
  },
});
