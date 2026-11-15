/**
 * Manual mock of `@controlvar/react-sdk` for jest.
 *
 * jest.config.js maps the package name here, so pages/components under test get these
 * hooks instead of the real provider. Tests configure return values with `__setExperiment`,
 * `__setFlag` and `__setUser`, and assert on `trackEventMock`.
 *
 * As in the real SDK, `useExperiment` assigns through the client's `assignExperiment`
 * (recorded in `assignExperimentMock`), and `getAssignments` returns only the experiments
 * assigned so far, so a test sees which component assigned what.
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

/** Every `assignExperiment(user, experimentKey)` call: `[userId, experimentKey]`. */
export const assignExperimentMock = jest.fn<void, [string, string]>();

export const DEFAULT_TEST_USER: UserContext = {
  userId: 'visitor-test-0001',
  attributes: { device: 'desktop', country: 'US', returning: false },
};

const state = {
  experiments: new Map<string, Partial<ExperimentAssignment>>(),
  flags: new Map<string, Partial<FeatureFlagEvaluation>>(),
  user: DEFAULT_TEST_USER,
  /** Keys assigned so far, in order: what the real client's cache would hold. */
  assigned: new Set<string>(),
};

export function defaultAssignment(experimentKey: string): ExperimentAssignment {
  return {
    experimentKey,
    variantKey: 'control',
    variantName: 'Control',
    variantId: null,
    isControl: true,
    configuration: null,
    loading: false,
    error: null,
  };
}

export function defaultFlag(flagKey: string): FeatureFlagEvaluation {
  return { flagKey, variant: null, isEnabled: false, config: null, loading: false, error: null };
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
  state.assigned.clear();
  trackEventMock.mockClear();
  assignExperimentMock.mockClear();
}

function assignmentFor(experimentKey: string): ExperimentAssignment {
  return { ...defaultAssignment(experimentKey), ...(state.experiments.get(experimentKey) ?? {}) };
}

function flagFor(flagKey: string): FeatureFlagEvaluation {
  return { ...defaultFlag(flagKey), ...(state.flags.get(flagKey) ?? {}) };
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

  async assignExperiment(user: UserContext, experimentKey: string): Promise<ExperimentAssignment> {
    assignExperimentMock(user.userId, experimentKey);
    state.assigned.add(experimentKey);
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

  /** The experiments assigned so far (never an unassigned one), as the real cache. */
  getAssignments(_userId: string): ExperimentAssignment[] {
    return Array.from(state.assigned).map(assignmentFor);
  }

  getEvaluatedFlags(_userId: string): string[] {
    return Array.from(state.flags.keys());
  }

  clearCache(): void {
    state.assigned.clear();
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
    // nothing cached in the mock
  }
}

const mockClient = new ExperimentationClient({ apiKey: 'test-key', baseUrl: 'http://localhost:8000' });

/* ----- Provider + hooks ----- */

export function ExperimentationProvider({ children }: { config: SdkConfig; user: UserContext; children: React.ReactNode }) {
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

/** Assigns on mount through the client, as the real hook does, and returns the configured answer. */
export function useExperiment(experimentKey: string): ExperimentAssignment {
  useEffect(() => {
    void mockClient.assignExperiment(state.user, experimentKey);
  }, [experimentKey]);
  return assignmentFor(experimentKey);
}

export function useTrackEvent(): (eventName: string, properties?: Record<string, unknown>, options?: TrackEventOptions) => void {
  return trackEventMock;
}

export function withExperimentation<P extends object>(Component: React.ComponentType<P>, _flagKey: string): React.ComponentType<P> {
  return Component;
}
