/**
 * OpenFeature Provider for Controlvar.
 *
 * Implements the @openfeature/server-sdk `Provider` interface on top of
 * `@controlvar/js-sdk`. Every evaluation is decided by the server
 * (`GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…&context=…`); the
 * JS SDK caches successful answers per user + flag for `cacheTtlMs` and never
 * caches failures.
 *
 * Resolution rules:
 *   boolean → `enabled`, for every flag (a string, number or json flag
 *   included): TARGETING_MATCH or CACHED when on, DISABLED when off.
 *   string, number, object → the flag's `value` when the flag serves that type
 *   (an object call reads a json flag): its on value when the flag is on for
 *   the user, its off value otherwise, an inactive flag included. The reason
 *   is the server's: `targeting_rule` → TARGETING_MATCH, `rollout` → SPLIT,
 *   `inactive` → DISABLED (anything else UNKNOWN), or CACHED when the SDK's
 *   cache answered; the variant is "on" or "off". Otherwise the caller's
 *   default with reason ERROR: TYPE_MISMATCH when the flag serves another
 *   type, GENERAL for `reason: "error"` or an answer with no value,
 *   PARSE_ERROR for a value that is not of the flag's type.
 *
 * Context mapping: `targetingKey` → `user_id`. Every other key of the
 * evaluation context is passed to the SDK as the user's attributes, which it
 * sends as the evaluate call's `context`; a Date, at any depth, as an ISO 8601
 * string. The SDK's cache key is user + flag, not the attributes: within
 * `cacheTtlMs` a user's answer is reused whatever attributes come with the
 * next call.
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

interface Failure {
  errorCode: ErrorCode;
  errorMessage: string;
}

type Lookup = { ok: true; evaluation: FlagEvaluation; cached: boolean } | ({ ok: false } & Failure);

/** The typed calls, and the flag type (`value_type`) each one reads. */
type ValueCall = 'string' | 'number' | 'object';

function servedTypeFor(call: ValueCall): 'string' | 'number' | 'json' {
  return call === 'object' ? 'json' : call;
}

function configField(config: unknown, field: string): unknown {
  if (config && typeof config === 'object' && !Array.isArray(config)) {
    return (config as Record<string, unknown>)[field];
  }
  return undefined;
}

/**
 * The evaluation context minus `targetingKey`: the user's attributes, as the JS SDK sends them
 * (`context`). A Date, at any depth, becomes its ISO 8601 string (an invalid Date, `null`, as
 * `JSON.stringify` writes it), so a local answer and the server's see the same text.
 */
function attributesOf(context: EvaluationContext): Record<string, unknown> {
  return plainEntries(Object.entries(context).filter(([key]) => key !== 'targetingKey'));
}

/**
 * An object of these entries, each value plain. `fromEntries` defines each key, so a `__proto__`
 * key stays an ordinary key.
 */
function plainEntries(entries: Array<[string, unknown]>): Record<string, unknown> {
  return Object.fromEntries(entries.map(([key, value]) => [key, plain(value)]));
}

function plain(value: unknown): unknown {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.toISOString();
  if (Array.isArray(value)) return value.map(plain);
  if (value !== null && typeof value === 'object') return plainEntries(Object.entries(value));
  return value;
}

/** The flag's type: the server's `value_type`, or the JSON type of its `value` when it sent none. */
function servedType(evaluation: FlagEvaluation): string | undefined {
  if (typeof evaluation.valueType === 'string') return evaluation.valueType;
  const { value } = evaluation;
  if (value === undefined || value === null) return undefined;
  return typeof value === 'object' ? 'json' : typeof value;
}

function isValueOfType(value: unknown, type: string): boolean {
  switch (type) {
    case 'string':
      return typeof value === 'string';
    case 'number':
      return typeof value === 'number' && Number.isFinite(value);
    case 'json':
      return value !== null && typeof value === 'object';
    default:
      return false;
  }
}

/** A flag type as the error messages name it. */
function describeType(type: string): string {
  switch (type) {
    case 'boolean':
      return 'a boolean';
    case 'string':
      return 'a string';
    case 'number':
      return 'a number';
    case 'json':
      return 'a JSON value';
    default:
      return `a value of type ${JSON.stringify(type.slice(0, 64))}`;
  }
}

function describeCall(call: ValueCall): string {
  return call === 'object' ? 'an object' : `a ${call}`;
}

/** OpenFeature's reason for the server's `reason` on a typed call. */
function reasonOf(serverReason: string | undefined): string {
  switch (serverReason) {
    case 'targeting_rule':
      return StandardResolutionReasons.TARGETING_MATCH;
    case 'rollout':
      return StandardResolutionReasons.SPLIT;
    case 'inactive':
      return StandardResolutionReasons.DISABLED;
    default:
      return StandardResolutionReasons.UNKNOWN;
  }
}

/** A copy that shares nothing with the SDK's cached evaluation (a JSON value came from JSON). */
function copyValue<T>(value: T): T {
  return value !== null && typeof value === 'object' ? (JSON.parse(JSON.stringify(value)) as T) : value;
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
   * are answered by this client too, so a keyless `client.track(...)` fans out
   * to them by the JS SDK's rule.
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
    const onReason = result.cached ? StandardResolutionReasons.CACHED : StandardResolutionReasons.TARGETING_MATCH;
    return {
      value: evaluation.enabled,
      reason: evaluation.enabled ? onReason : StandardResolutionReasons.DISABLED,
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
    return this.resolveValue(flagKey, defaultValue, context, 'string');
  }

  async resolveNumberEvaluation(
    flagKey: string,
    defaultValue: number,
    context: EvaluationContext = {},
    _logger?: Logger,
  ): Promise<ResolutionDetails<number>> {
    return this.resolveValue(flagKey, defaultValue, context, 'number');
  }

  async resolveObjectEvaluation<T extends JsonValue>(
    flagKey: string,
    defaultValue: T,
    context: EvaluationContext = {},
    _logger?: Logger,
  ): Promise<ResolutionDetails<T>> {
    return this.resolveValue(flagKey, defaultValue, context, 'object');
  }

  // ---------------------------------------------------------------------------
  // Internals
  // ---------------------------------------------------------------------------

  /**
   * The string, number and object calls' one body: the flag's value when it serves the type
   * `call` reads (a JSON value as a new copy each call), and otherwise `defaultValue` with an error.
   */
  private async resolveValue<T>(
    flagKey: string,
    defaultValue: T,
    context: EvaluationContext,
    call: ValueCall,
  ): Promise<ResolutionDetails<T>> {
    const result = await this.evaluate(flagKey, context);
    if (!result.ok) return this.errorDetails(defaultValue, result);
    const { evaluation } = result;
    const flag = JSON.stringify(String(flagKey));
    const noValue = {
      errorCode: ErrorCode.GENERAL,
      errorMessage:
        `Flag ${flag} came back without a value (from a server without flag values, or one that ` +
        `cannot serve this flag's stored value)`,
    };

    if (evaluation.reason === 'error') {
      return this.errorDetails(defaultValue, {
        errorCode: ErrorCode.GENERAL,
        errorMessage: `The server could not evaluate flag ${flag} for this user`,
      });
    }
    const served = servedType(evaluation);
    if (served === undefined) return this.errorDetails(defaultValue, noValue);
    const wanted = servedTypeFor(call);
    if (served !== wanted) {
      return this.errorDetails(defaultValue, {
        errorCode: ErrorCode.TYPE_MISMATCH,
        errorMessage: `Flag ${flag} serves ${describeType(served)}; it was resolved as ${describeCall(call)}`,
      });
    }
    const { value } = evaluation;
    if (value === undefined || value === null) return this.errorDetails(defaultValue, noValue);
    if (!isValueOfType(value, wanted)) {
      return this.errorDetails(defaultValue, {
        errorCode: ErrorCode.PARSE_ERROR,
        errorMessage: `Flag ${flag} serves ${describeType(wanted)}, but the server's value is not one`,
      });
    }
    return {
      value: copyValue(value) as T,
      reason: result.cached ? StandardResolutionReasons.CACHED : reasonOf(evaluation.reason),
      variant: evaluation.enabled ? 'on' : 'off',
      flagMetadata: this.metadataFor(evaluation),
    };
  }

  /**
   * Resolve `targetingKey` → `user_id`, the rest of the context → the user's attributes, and
   * evaluate through the JS SDK (cached per user + flag, whatever the attributes).
   */
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
      const attributes = attributesOf(context ?? {});
      const evaluation = await this.client.evaluateFlag(flagKey, { userId, attributes });
      return { ok: true, evaluation, cached };
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

  private errorDetails<T>(value: T, failure: Failure): ResolutionDetails<T> {
    return {
      value,
      reason: StandardResolutionReasons.ERROR,
      errorCode: failure.errorCode,
      errorMessage: failure.errorMessage,
    };
  }
}
