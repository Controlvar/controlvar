/**
 * @controlvar/react-native-sdk
 *
 * React Native SDK for Controlvar. Flag evaluation and
 * experiment assignment are decided by the server; results are cached in
 * memory and (optionally) persisted to AsyncStorage as an offline fallback.
 *
 * Key exports:
 *   - {@link ExperimentationClient} — core client for flag evaluation, assignments, and tracking.
 *   - {@link ExperimentationProvider} — React context provider.
 *   - {@link useFlag} — hook for feature flag evaluation.
 *   - {@link useExperiment} — hook for experiment assignment.
 *   - {@link useExperimentationClient} — hook for raw client access.
 *   - {@link hashUser} — the cross-SDK consistent hash (compatibility utility only).
 */

// Core client
export { ExperimentationClient, ApiError } from './client';

// Consistent hash (utility only — nothing in the SDK uses it to pick a variant)
export { hashUser } from './hash';

// Cache & storage
export { EvaluationCache } from './cache';
export { OfflineStorage } from './storage';

// Context & provider
export { ExperimentationContext, useExperimentationContext } from './context/ExperimentationContext';
export { ExperimentationProvider } from './context/ExperimentationProvider';

// Hooks
export { useFlag } from './hooks/useFlag';
export { useExperiment } from './hooks/useExperiment';
export { useExperimentationClient } from './hooks/useExperimentationClient';

// Types
export type {
  SdkConfig,
  SwallowedOperation,
  FlagEvaluation,
  Assignment,
  TrackOptions,
  TrackEvent,
  BatchResult,
  FlagState,
  ExperimentState,
  CacheEntry,
  AssignResponse,
  FlagEvaluateResponse,
  BatchResponse,
  TrackBody,
} from './types';
export type { ExperimentationContextValue } from './context/ExperimentationContext';
export type { ExperimentationProviderProps } from './context/ExperimentationProvider';
