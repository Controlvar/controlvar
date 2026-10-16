/**
 * OpenFeature Provider for Controlvar.
 *
 * Implements the @openfeature/server-sdk `Provider` interface on top of
 * `@controlvar/js-sdk`. Every evaluation is decided by the
 * server (`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…`); the
 * JS SDK caches successful answers per user + flag for `cacheTtlMs` and never
 * caches failures.
 *
 * Resolution rules:
 *   boolean → `enabled`
 *   string, number, object → the caller's default with reason DEFAULT. The
 *   evaluate endpoint returns `config: null` for every flag, so there is
 *   nothing to read. The provider would read `config.variant` (or `config`
 *   itself, for a string), `config.value` (or `config` itself, for a number)
 *   and `config` (for an object) if the server ever sent one.
 *   A disabled flag yields the default with reason DISABLED for non-boolean types.
 *
 * Context mapping: `targetingKey` → `user_id`. Other context attributes are
 * NOT sent: the evaluate endpoint takes a `context`, which this provider does
 * not send yet.
 *
 * Experiments and event tracking are not OpenFeature concepts; use
 * `provider.client` (the underlying JS SDK client) for `getAssignment`,
 * `getVariant`, `track` and `trackBatch`.
 */

import {
  ExperimentationClient,
  consistentHash,
  type FlagEvaluation,
} from '@controlvar/js-sdk';
import {
  ErrorCode,
  StandardResolutionReasons,
  type EvaluationContext,
  type Hook,
  type JsonValue,
  type Logger,
  type Provider,
  type ProviderMetadata,
  type ResolutionDetails,
} from '@openfeature/server-sdk';
import type { ExperimentationFlagMetadata, ExperimentationProviderOptions } from './types';

export type { ExperimentationProviderOptions } from './types';

const DEFAULT_BASE_URL = 'http://localhost:8000';
const DEFAULT_CACHE_TTL_MS = 300_000;
const DEFAULT_TIMEOUT_MS = 5_000;

type Lookup =
  | { ok: true; evaluation: FlagEvaluation; reason: string }
  | { ok: false; errorCode: ErrorCode; errorMessage: string };

function configField(config: unknown, field: string): unknown {
  if (config && typeof config === 'object' && !Array.isArray(config)) {
    return (config as Record<string, unknown>)[field];
  }
  return undefined;
}

export class ExperimentationProvider implements Provider {
  readonly metadata: ProviderMetadata = {
    name: 'controlvar-provider',
  };

  hooks?: Hook[];

  /**
   * The underlying JS SDK client. Use it for experiment assignment
   * (`getAssignment` / `getVariant`) and event tracking (`track` / `trackBatch`),
   * which have no OpenFeature equivalent. Flags evaluated through OpenFeature
   * are cached here too, so a keyless `client.track(...)` fans out to them.
   */
  readonly client: ExperimentationClient;

  constructor(options: ExperimentationProviderOptions) {
    if (options.client) {
      this.client = options.client;
      return;
    }
    if (!options.apiKey) {
      throw new Error('ExperimentationProvider: apiKey is required');
    }
    this.client = new ExperimentationClient({
      apiUrl: options.baseUrl ?? DEFAULT_BASE_URL,
      apiKey: options.apiKey,
      cacheTtlMs: options.cacheTtlMs ?? DEFAULT_CACHE_TTL_MS,
      timeoutMs: options.timeout ?? DEFAULT_TIMEOUT_MS,
      fetch: options.fetch,
    });
  }

  /**
   * Called by the OpenFeature SDK after the provider is registered.
   * There is nothing to pre-fetch: flags are evaluated per user on demand.
   */
  async initialize(_context?: EvaluationContext): Promise<void> {
    // no-op
  }

  /** Called when the provider is replaced or the SDK shuts down. Drops cached evaluations. */
  async onClose(): Promise<void> {
    this.client.clearCache();
  }

  /**
   * @deprecated There is no SDK-facing "list all flags" endpoint any more, so
   * nothing can be refreshed eagerly. Kept for API compatibility: it clears the
   * evaluation cache so the next resolution of every flag hits the server.
   */
  async refreshFlags(): Promise<void> {
    this.client.clearCache();
  }

  /**
   * Cross-SDK consistent hash (`MD5("{userId}:{flagKey}")` → first 4 bytes LE ÷ 2^32).
   * Exposed as a utility only; the server decides every evaluation.
   */
  hashUser(userId: string, flagKey: string): number {
    return consistentHash(userId, flagKey);
  }

  // ---------------------------------------------------------------------------
  // OpenFeature Provider interface methods
  // ---------------------------------------------------------------------------

  async resolveBooleanEvaluation(
    flagKey: string,
    defaultValue: boolean,
    context: EvaluationContext = {},
    _logger?: Logger,
  ): Promise<ResolutionDetails<boolean>> {
    const result = await this.evaluate(flagKey, context);
    if (!result.ok) return this.errorDetails(defaultValue, result);
    const { evaluation } = result;
    return {
      value: evaluation.enabled,
      reason: evaluation.enabled ? result.reason : StandardResolutionReasons.DISABLED,
      variant: this.variantOf(evaluation),
      flagMetadata: this.metadataFor(evaluation),
    };
  }

  async resolveStringEvaluation(
    flagKey: string,
    defaultValue: string,
    context: EvaluationContext = {},
    _logger?: Logger,
  ): Promise<ResolutionDetails<string>> {
    const result = await this.evaluate(flagKey, context);
    if (!result.ok) return this.errorDetails(defaultValue, result);
    const { evaluation } = result;
    if (!evaluation.enabled) return this.disabledDetails(defaultValue, evaluation);

    const variant = configField(evaluation.config, 'variant');
    const value =
      typeof variant === 'string'
        ? variant
        : typeof evaluation.config === 'string'
          ? evaluation.config
          : undefined;
    if (value === undefined) return this.defaultDetails(defaultValue, evaluation);
    return {
      value,
      reason: result.reason,
      variant: this.variantOf(evaluation),
      flagMetadata: this.metadataFor(evaluation),
    };
  }

  async resolveNumberEvaluation(
    flagKey: string,
    defaultValue: number,
    context: EvaluationContext = {},
    _logger?: Logger,
  ): Promise<ResolutionDetails<number>> {
    const result = await this.evaluate(flagKey, context);
    if (!result.ok) return this.errorDetails(defaultValue, result);
    const { evaluation } = result;
    if (!evaluation.enabled) return this.disabledDetails(defaultValue, evaluation);

    const field = configField(evaluation.config, 'value');
    const value =
      typeof field === 'number'
        ? field
        : typeof evaluation.config === 'number'
          ? evaluation.config
          : undefined;
    if (value === undefined) return this.defaultDetails(defaultValue, evaluation);
    return {
      value,
      reason: result.reason,
      variant: this.variantOf(evaluation),
      flagMetadata: this.metadataFor(evaluation),
    };
  }

  async resolveObjectEvaluation<T extends JsonValue>(
    flagKey: string,
    defaultValue: T,
    context: EvaluationContext = {},
    _logger?: Logger,
  ): Promise<ResolutionDetails<T>> {
    const result = await this.evaluate(flagKey, context);
    if (!result.ok) return this.errorDetails(defaultValue, result);
    const { evaluation } = result;
    if (!evaluation.enabled) return this.disabledDetails(defaultValue, evaluation);

    if (evaluation.config === null || typeof evaluation.config !== 'object') {
      return this.defaultDetails(defaultValue, evaluation);
    }
    return {
      value: evaluation.config as T,
      reason: result.reason,
      variant: this.variantOf(evaluation),
      flagMetadata: this.metadataFor(evaluation),
    };
  }

  // ---------------------------------------------------------------------------
  // Internals
  // ---------------------------------------------------------------------------

  /** Resolve `targetingKey` → `user_id` and evaluate through the JS SDK (cached per user + flag). */
  private async evaluate(flagKey: string, context: EvaluationContext | undefined): Promise<Lookup> {
    const userId = context?.targetingKey;
    if (typeof userId !== 'string' || userId.length === 0) {
      return {
        ok: false,
        errorCode: ErrorCode.TARGETING_KEY_MISSING,
        errorMessage: 'targetingKey is required: it is sent to the server as user_id',
      };
    }

    const cached = this.client.getEvaluatedFlags(userId).includes(flagKey);
    try {
      const evaluation = await this.client.evaluateFlag(flagKey, { userId });
      return {
        ok: true,
        evaluation,
        reason: cached ? StandardResolutionReasons.CACHED : StandardResolutionReasons.TARGETING_MATCH,
      };
    } catch (err) {
      const status = (err as { status?: number })?.status;
      const code = (err as { code?: string })?.code;
      const message = err instanceof Error ? err.message : String(err);
      if (status === 404) {
        return {
          ok: false,
          errorCode: ErrorCode.FLAG_NOT_FOUND,
          errorMessage: `Flag "${flagKey}" not found`,
        };
      }
      if (code === 'INVALID_RESPONSE') {
        return { ok: false, errorCode: ErrorCode.PARSE_ERROR, errorMessage: message };
      }
      return { ok: false, errorCode: ErrorCode.GENERAL, errorMessage: message };
    }
  }

  private variantOf(evaluation: FlagEvaluation): string | undefined {
    const variant = configField(evaluation.config, 'variant');
    return typeof variant === 'string' ? variant : undefined;
  }

  private metadataFor(evaluation: FlagEvaluation): ExperimentationFlagMetadata {
    return { flagKey: evaluation.key, enabled: evaluation.enabled };
  }

  private errorDetails<T>(value: T, result: Extract<Lookup, { ok: false }>): ResolutionDetails<T> {
    return {
      value,
      reason: StandardResolutionReasons.ERROR,
      errorCode: result.errorCode,
      errorMessage: result.errorMessage,
    };
  }

  private disabledDetails<T>(value: T, evaluation: FlagEvaluation): ResolutionDetails<T> {
    return {
      value,
      reason: StandardResolutionReasons.DISABLED,
      flagMetadata: this.metadataFor(evaluation),
    };
  }

  private defaultDetails<T>(value: T, evaluation: FlagEvaluation): ResolutionDetails<T> {
    return {
      value,
      reason: StandardResolutionReasons.DEFAULT,
      flagMetadata: this.metadataFor(evaluation),
    };
  }
}
