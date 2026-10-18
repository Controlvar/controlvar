/**
 * Manual mock of `@controlvar/react-sdk` for jest.
 *
 * jest.config.js maps the package name here, so components under test get these
 * hooks instead of the real provider. Tests configure return values with
 * `__setExperiment`, `__setFlag` and `__setUser`, assert on `trackEventMock`, and
 * can observe provider (re)mounts through `providerMounts` / `lastProviderProps`.
 */
import React, { useEffect } from 'react';
import type {
  ExperimentAssignment,
  FeatureFlagEvaluation,
  SdkConfig,
  TrackEventOptions,
  UserContext,
} from '@controlvar/react-sdk';

type TrackArgs = [string, Record<string, unknown>?, TrackEventOptions?];

export const trackEventMock = jest.fn<void, TrackArgs>();

export const DEFAULT_TEST_USER: UserContext = {
  userId: 'sp-test-device',
  attributes: { os: 'iOS', os_version: '17.4.0', region: 'US', tier: 'premium', employee: false },
};

/** Every `{config, user}` the mock provider was mounted with, in order. */
export const providerMounts: Array<{ config: SdkConfig; user: UserContext }> = [];
/** How many times `clearCache()` was called on the mock client. */
export const clearCacheMock = jest.fn();

const state = {
  experiments: new Map<string, Partial<ExperimentAssignment>>(),
  flags: new Map<string, Partial<FeatureFlagEvaluation>>(),
  user: DEFAULT_TEST_USER,
};

export function defaultAssignment(experimentKey: string): ExperimentAssignment {
  return {
    experimentKey,
    variantKey: 'control',
    variantName: 'control',
    variantId: null,
    isControl: true,
    configuration: null,
    loading: false,
    error: null,
    assigned: true,
    reason: 'assigned',
  };
}

export function defaultFlag(flagKey: string): FeatureFlagEvaluation {
  return { flagKey, variant: null, isEnabled: false, config: null, loading: false, error: null, reason: 'rollout' };
}

export function __setExperiment(experimentKey: string, overrides: Partial<ExperimentAssignment>): void {
  state.experiments.set(experimentKey, overrides);
}

export function __setFlag(flagKey: string, overrides: Partial<FeatureFlagEvaluation>): void {
  state.flags.set(flagKey, overrides);
}

export function __setUser(user: UserContext): void {
  state.user = user;
}

export function __reset(): void {
  state.experiments.clear();
  state.flags.clear();
  state.user = DEFAULT_TEST_USER;
  trackEventMock.mockClear();
  clearCacheMock.mockClear();
  providerMounts.length = 0;
}

function assignmentFor(experimentKey: string): ExperimentAssignment {
  return { ...defaultAssignment(experimentKey), ...(state.experiments.get(experimentKey) ?? {}) };
}

function flagFor(flagKey: string): FeatureFlagEvaluation {
  const evaluation = { ...defaultFlag(flagKey), ...(state.flags.get(flagKey) ?? {}) };
  if (evaluation.isEnabled && evaluation.variant === null) evaluation.variant = 'on';
  return evaluation;
}

/* ----- Client classes (minimal, never hit the network) ----- */

export class ExperimentationClient {
  readonly config: SdkConfig;

  constructor(config: SdkConfig) {
    this.config = config;
  }

  async evaluateFeatureFlag(_user: UserContext, flagKey: string): Promise<string | null> {
    return flagFor(flagKey).variant;
  }

  async evaluateFeatureFlagDetailed(_user: UserContext, flagKey: string): Promise<FeatureFlagEvaluation> {
    return flagFor(flagKey);
  }

  async assignExperiment(_user: UserContext, experimentKey: string): Promise<ExperimentAssignment> {
    return assignmentFor(experimentKey);
  }

  async trackEvent(
    _userId: string,
    eventName: string,
    properties?: Record<string, unknown>,
    options?: TrackEventOptions,
  ): Promise<void> {
    trackEventMock(eventName, properties, options);
  }

  getAssignments(_userId: string): ExperimentAssignment[] {
    return Array.from(state.experiments.keys()).map(assignmentFor);
  }

  getEvaluatedFlags(_userId: string): string[] {
    return Array.from(state.flags.keys());
  }

  clearCache(): void {
    clearCacheMock();
  }
}

export class ServerClient {
  async evaluateFeatureFlag(flagKey: string, _user: UserContext): Promise<FeatureFlagEvaluation> {
    return flagFor(flagKey);
  }

  async assignExperiment(experimentKey: string, _user: UserContext): Promise<ExperimentAssignment> {
    return assignmentFor(experimentKey);
  }

  async getAll(flagKeys: string[], _user: UserContext): Promise<Record<string, FeatureFlagEvaluation>> {
    return Object.fromEntries(flagKeys.map((k) => [k, flagFor(k)]));
  }

  clearCache(): void {
    clearCacheMock();
  }
}

const mockClient = new ExperimentationClient({ apiKey: 'test-key', baseUrl: 'http://localhost:8000' });

/* ----- Provider + hooks ----- */

export function ExperimentationProvider({
  config,
  user,
  children,
}: {
  config: SdkConfig;
  user: UserContext;
  children: React.ReactNode;
}) {
  // Record each mount so tests can prove the page re-keys the provider when the
  // device changes (a re-key = unmount + mount, i.e. a new entry here).
  useEffect(() => {
    providerMounts.push({ config, user });
    state.user = user;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <>{children}</>;
}

export function useExperimentation(): { client: ExperimentationClient; user: UserContext } {
  return { client: mockClient, user: state.user };
}

export function useFeatureFlag(flagKey: string): FeatureFlagEvaluation {
  return flagFor(flagKey);
}

export function useVariant(flagKey: string): string | null {
  return flagFor(flagKey).variant;
}

export function useMultipleFlags(flagKeys: string[]): Record<string, FeatureFlagEvaluation | null> {
  return Object.fromEntries(flagKeys.map((k) => [k, flagFor(k)]));
}

export function useExperiment(experimentKey: string): ExperimentAssignment {
  return assignmentFor(experimentKey);
}

export function useTrackEvent(): (eventName: string, properties?: Record<string, unknown>, options?: TrackEventOptions) => void {
  return trackEventMock;
}

export function withExperimentation<P extends object>(Component: React.ComponentType<P>, _flagKey: string): React.ComponentType<P> {
  return Component;
}
