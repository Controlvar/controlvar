/**
 * Local-evaluation runtime: fetches and refreshes the ruleset, answers flags from it, and reports
 * how many evaluations it answered so the server's safety monitoring keeps its denominator.
 *
 * Failure handling (after the first successful load the SDK "has a ruleset"):
 *
 * | Ruleset response                      | Without a ruleset            | With a ruleset                                   |
 * |---------------------------------------|------------------------------|--------------------------------------------------|
 * | 200 / 304                             | load                         | swap, or keep                                    |
 * | 5xx, network error, timeout           | stay on the server; back off | keep serving it; `onError`; back off (≤ 5 min)   |
 * | 200 that is not a valid ruleset       | stay on the server           | keep serving it; `onError`; back off             |
 * | 429                                   | wait `Retry-After`           | keep serving it; wait `Retry-After`              |
 * | 401 / 403                             | stay on the server           | DISCARD it; every flag goes to the server        |
 * | 404, or an unknown `schema`/`bucketing` | stay on the server         | DISCARD it; every flag goes to the server        |
 *
 * 401/403/404 and an unknown format are reported once (until a refresh succeeds) and retried every
 * 10 minutes. "Goes to the server" means the call is made exactly as in server mode.
 *
 * Only the regular refresh interval is jittered (±10%). A backoff step, the 5-minute backoff cap,
 * a `Retry-After` wait and the 10-minute retry are exact: `Retry-After` is a floor and the caps are
 * ceilings.
 */
import { ExperimentationError, asExperimentationError } from './errors';
import {
  DEFER,
  Defer,
  IndexedRuleset,
  LocalAnswer,
  evaluateLocally,
  indexRuleset,
  isSupportedFormat,
  toWireContext,
} from './evaluator';

export const RULESET_PATH = '/api/v1/sdk/ruleset';
export const EVALUATIONS_PATH = '/api/v1/tracking/evaluations';
/** The first server release that serves the ruleset and accepts evaluation counts. */
export const MIN_SERVER_RELEASE = '0.11.0';

export const DEFAULT_REFRESH_INTERVAL_MS = 30_000;
export const MIN_REFRESH_INTERVAL_MS = 5_000;
/** Longest wait between refreshes while the server is failing. */
export const MAX_BACKOFF_MS = 300_000;
/** Wait between refreshes after 401, 403, 404 or an unknown ruleset format. */
export const REFUSED_RETRY_MS = 600_000;
/** How often evaluation counts are sent. */
export const FLUSH_INTERVAL_MS = 60_000;

/** Server limits of `POST /api/v1/tracking/evaluations`. */
const MAX_REPORT_ENTRIES = 1000;
const MAX_ENTRY_COUNT = 1_000_000;
const MAX_WINDOW_MS = 10 * 60_000;

export type LocalOperation = 'refresh' | 'flush' | 'evaluate';

/** The delay before the next refresh, and whether it is the regular interval (jittered). */
interface NextRefresh {
  ms: number;
  jitter: boolean;
}

export interface LocalRequestInit {
  method?: 'GET' | 'POST';
  body?: unknown;
  headers?: Record<string, string>;
  /** Do not retry a 429 inside the request; the caller handles it. */
  retried?: boolean;
}

/** What the runtime needs from the client: its HTTP plumbing and its error reporting. */
export interface LocalTransport {
  request(path: string, init: LocalRequestInit): Promise<Response>;
  httpError(response: Response, path: string, method: string): Promise<ExperimentationError>;
  report(error: ExperimentationError, operation: LocalOperation): void;
}

export interface LocalOptions {
  refreshIntervalMs: number;
  /** Stop answering locally when the ruleset has not been refreshed for this long. */
  maxStaleMs?: number;
}

/** `ready()`'s answer. It never rejects. */
export type ReadyResult =
  | { ok: true; rulesetVersion: string | null }
  | { ok: false; error: ExperimentationError };

export interface LocalEvaluationStatus {
  /** The configured mode. */
  evaluation: 'server' | 'local';
  /** Whether flags are being answered locally right now (always `true` in server mode). */
  ready: boolean;
  /** The version (ETag) of the ruleset in use, or `null` when there is none. */
  rulesetVersion: string | null;
  /** When the ruleset was last fetched or confirmed unchanged, or `null`. */
  lastRefreshAt: Date | null;
  /** The last refresh failure, cleared by a successful refresh. */
  lastError: ExperimentationError | null;
  /** Active flags the server evaluates even in local mode (their rules are not portable). */
  serverEvaluatedFlags: string[];
}

interface Tally {
  count: number;
  enabled: number;
}

function jittered(next: NextRefresh): number {
  return next.jitter ? Math.round(next.ms * (0.9 + Math.random() * 0.2)) : next.ms;
}

function retryAfterMs(response: Response): number | null {
  const header = typeof response.headers?.get === 'function' ? response.headers.get('Retry-After') : null;
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(seconds * 1000, 0);
  const date = Date.parse(header);
  return Number.isNaN(date) ? null : Math.max(date - Date.now(), 0);
}

function unref(timer: ReturnType<typeof setTimeout>): void {
  const maybe = timer as unknown as { unref?: () => void };
  if (typeof maybe.unref === 'function') maybe.unref();
}

export class LocalEvaluation {
  private ruleset: IndexedRuleset | null = null;
  private etag: string | null = null;
  private lastRefreshAt: number | null = null;
  private lastError: ExperimentationError | null = null;
  private failures = 0;
  /** The kind of refusal already reported (401, 403, 404, format), until a success. */
  private refusal: string | null = null;
  private closed = false;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private firstAttempt: Promise<void> | null = null;
  private tallies = new Map<string, Tally>();
  private windowStart: number | null = null;
  private flushing: Promise<void> = Promise.resolve();

  constructor(
    private readonly transport: LocalTransport,
    private readonly options: LocalOptions
  ) {}

  /** Start the first fetch and the timers. Never throws. */
  start(): void {
    this.firstAttempt = this.refresh().then(next => this.schedule(next));
    this.scheduleFlush();
  }

  // ─── Answers ───────────────────────────────────────────────────────────────

  /** The ruleset to answer from, or `null` (closed, never loaded, discarded, or too stale). */
  private usable(): IndexedRuleset | null {
    if (this.closed || !this.ruleset) return null;
    const maxStale = this.options.maxStaleMs;
    if (maxStale !== undefined && (this.lastRefreshAt === null || Date.now() - this.lastRefreshAt > maxStale)) {
      return null;
    }
    return this.ruleset;
  }

  /** One flag, answered locally and counted, or DEFER. */
  answer(flagKey: string, userId: string, attributes: unknown): LocalAnswer | Defer {
    const ruleset = this.usable();
    if (!ruleset) return DEFER;
    try {
      const context = toWireContext(attributes);
      if (context === DEFER) return DEFER;
      const answer = evaluateLocally(ruleset, flagKey, userId, context);
      if (answer !== DEFER) this.count(flagKey, answer.enabled);
      return answer;
    } catch (err) {
      this.evaluationFailed(err);
      return DEFER;
    }
  }

  /**
   * Every active flag for a user (inactive flags omitted, as the server does), or DEFER when any
   * active flag cannot be answered locally for this context.
   */
  answerAll(userId: string, attributes: unknown): Record<string, boolean> | Defer {
    const ruleset = this.usable();
    if (!ruleset) return DEFER;
    try {
      const context = toWireContext(attributes);
      if (context === DEFER) return DEFER;
      const flags: Record<string, boolean> = {};
      for (const [key, flag] of ruleset.flags) {
        if (!flag.active) continue;
        const answer = evaluateLocally(ruleset, key, userId, context);
        if (answer === DEFER) return DEFER;
        flags[key] = answer.enabled;
      }
      for (const key of Object.keys(flags)) this.count(key, flags[key]);
      return flags;
    } catch (err) {
      this.evaluationFailed(err);
      return DEFER;
    }
  }

  /** A bug in local evaluation: reported as `'evaluate'`; the call still goes to the server. */
  private evaluationFailed(err: unknown): void {
    const error = asExperimentationError(err);
    this.transport.report(
      new ExperimentationError(`Local evaluation failed; asking the server instead: ${error.message}`, {
        code: 'INVALID_RESPONSE',
        cause: err,
      }),
      'evaluate'
    );
  }

  // ─── Lifecycle ─────────────────────────────────────────────────────────────

  async ready(timeoutMs?: number): Promise<ReadyResult> {
    if (!this.usable() && this.firstAttempt) {
      let timer: ReturnType<typeof setTimeout> | null = null;
      const waits: Array<Promise<unknown>> = [this.firstAttempt];
      if (timeoutMs !== undefined) {
        waits.push(new Promise(resolve => (timer = setTimeout(resolve, Math.max(timeoutMs, 0)))));
      }
      await Promise.race(waits);
      if (timer) clearTimeout(timer);
    }
    const ruleset = this.usable();
    if (ruleset) return { ok: true, rulesetVersion: ruleset.version };
    const error =
      this.lastError ??
      new ExperimentationError(
        this.closed ? 'The client is closed' : 'No flag ruleset has been loaded yet',
        { code: this.closed ? 'NETWORK_ERROR' : 'TIMEOUT' }
      );
    return { ok: false, error };
  }

  status(): LocalEvaluationStatus {
    const ruleset = this.usable();
    const remote: string[] = [];
    if (this.ruleset) {
      for (const [key, flag] of this.ruleset.flags) {
        if (flag.active && flag.evaluation !== 'local') remote.push(key);
      }
    }
    return {
      evaluation: 'local',
      ready: ruleset !== null,
      rulesetVersion: this.ruleset ? this.ruleset.version : null,
      lastRefreshAt: this.lastRefreshAt === null ? null : new Date(this.lastRefreshAt),
      lastError: this.lastError,
      serverEvaluatedFlags: remote.sort(),
    };
  }

  /** Stop refreshing, send the remaining counts, and answer every later call on the server. */
  async close(): Promise<void> {
    if (this.closed) return this.flushing;
    this.closed = true;
    if (this.pollTimer) clearTimeout(this.pollTimer);
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.pollTimer = null;
    this.flushTimer = null;
    await this.flush();
  }

  // ─── Refresh ───────────────────────────────────────────────────────────────

  private schedule(next: NextRefresh): void {
    if (this.closed) return;
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      void this.refresh().then(after => this.schedule(after));
    }, jittered(next));
    unref(this.pollTimer);
  }

  private get interval(): number {
    return this.options.refreshIntervalMs;
  }

  /** One refresh. Resolves to the delay before the next; never rejects. */
  async refresh(): Promise<NextRefresh> {
    const method = 'GET';
    let response: Response;
    try {
      const headers: Record<string, string> = {};
      if (this.etag) headers['If-None-Match'] = this.etag;
      response = await this.transport.request(RULESET_PATH, { method, headers, retried: true });
    } catch (err) {
      return this.transient(asExperimentationError(err));
    }
    if (this.closed) return { ms: this.interval, jitter: false };

    try {
      const status = response.status;
      if (status === 304) {
        if (this.ruleset) return this.succeeded();
        this.etag = null;
        return this.transient(
          new ExperimentationError(`Unexpected 304 without a ruleset (GET ${RULESET_PATH})`, {
            code: 'INVALID_RESPONSE',
            status,
          })
        );
      }
      if (status >= 200 && status < 300) return await this.load(response);
      if (status === 401 || status === 403) {
        const error = await this.transport.httpError(response, RULESET_PATH, method);
        const body = error.body as { detail?: unknown } | undefined;
        const detail = body && typeof body.detail === 'string' ? body.detail : error.message;
        return this.refused(
          String(status),
          new ExperimentationError(
            `The API key was refused (${status}) fetching the flag ruleset: ${detail} ` +
              "Local evaluation needs a key with the 'sdk:ruleset' scope whose owner can change " +
              'feature flags. Until a refresh succeeds every flag is evaluated by the server; the ' +
              'SDK retries every 10 minutes.',
            { code: 'HTTP_ERROR', status, body: error.body }
          )
        );
      }
      if (status === 404) {
        return this.refused(
          '404',
          new ExperimentationError(
            `The server has no ${RULESET_PATH} (404). Local evaluation needs Controlvar ` +
              `${MIN_SERVER_RELEASE} or later: upgrade the server, or leave evaluation at 'server'. ` +
              'Until then every flag is evaluated by the server.',
            { code: 'HTTP_ERROR', status }
          )
        );
      }
      if (status === 429) {
        const error = await this.transport.httpError(response, RULESET_PATH, method);
        this.lastError = error;
        this.transport.report(error, 'refresh');
        // Retry-After is a floor: never earlier than the server asked, nor than the interval.
        return { ms: Math.max(retryAfterMs(response) ?? this.interval, this.interval), jitter: false };
      }
      return this.transient(await this.transport.httpError(response, RULESET_PATH, method));
    } catch (err) {
      return this.transient(asExperimentationError(err));
    }
  }

  private async load(response: Response): Promise<NextRefresh> {
    let body: unknown;
    try {
      body = await response.json();
    } catch (err) {
      return this.transient(
        new ExperimentationError(`Invalid JSON in the flag ruleset (GET ${RULESET_PATH})`, {
          code: 'INVALID_RESPONSE',
          status: response.status,
          cause: err,
        })
      );
    }
    if (!isSupportedFormat(body)) {
      const doc = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
      return this.refused(
        'format',
        new ExperimentationError(
          `The server sent a flag ruleset in a format this SDK does not understand (schema ` +
            `${JSON.stringify(doc.schema)}, bucketing ${JSON.stringify(doc.bucketing)}). Every flag ` +
            'is evaluated by the server; upgrade @controlvar/js-sdk to evaluate locally.',
          { code: 'INVALID_RESPONSE', status: response.status }
        )
      );
    }
    const indexed = indexRuleset(body);
    if (!indexed) {
      return this.transient(
        new ExperimentationError(`The flag ruleset is malformed (GET ${RULESET_PATH})`, {
          code: 'INVALID_RESPONSE',
          status: response.status,
        })
      );
    }
    this.ruleset = indexed;
    const etag = typeof response.headers?.get === 'function' ? response.headers.get('ETag') : null;
    this.etag = etag || `"${indexed.version}"`;
    return this.succeeded();
  }

  private succeeded(): NextRefresh {
    this.lastRefreshAt = Date.now();
    this.lastError = null;
    this.failures = 0;
    this.refusal = null;
    return { ms: this.interval, jitter: true };
  }

  /** A failure that may pass: keep whatever ruleset there is, report, back off. */
  private transient(error: ExperimentationError): NextRefresh {
    this.lastError = error;
    this.failures += 1;
    this.transport.report(error, 'refresh');
    return { ms: Math.min(this.interval * 2 ** (this.failures - 1), MAX_BACKOFF_MS), jitter: false };
  }

  /** The server refused: discard the ruleset (so nothing is answered locally), report once. */
  private refused(kind: string, error: ExperimentationError): NextRefresh {
    this.ruleset = null;
    this.etag = null;
    this.lastError = error;
    this.failures = 0;
    if (this.refusal !== kind) {
      this.refusal = kind;
      this.transport.report(error, 'refresh');
    }
    return { ms: REFUSED_RETRY_MS, jitter: false };
  }

  // ─── Evaluation counts ─────────────────────────────────────────────────────

  private count(flagKey: string, enabled: boolean): void {
    if (this.windowStart === null) this.windowStart = Date.now();
    let tally = this.tallies.get(flagKey);
    if (!tally) {
      tally = { count: 0, enabled: 0 };
      this.tallies.set(flagKey, tally);
    }
    tally.count += 1;
    if (enabled) tally.enabled += 1;
  }

  private scheduleFlush(): void {
    if (this.closed) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      void this.flush().then(() => this.scheduleFlush());
    }, FLUSH_INTERVAL_MS);
    unref(this.flushTimer);
  }

  /** Send the counts gathered since the last flush. Never rejects; failures go to `onError`. */
  flush(): Promise<void> {
    const run = this.flushing.then(() => this.send());
    this.flushing = run;
    return run;
  }

  private async send(): Promise<void> {
    if (this.tallies.size === 0) return;
    const end = Date.now();
    const start = Math.max(this.windowStart ?? end, end - MAX_WINDOW_MS);
    const tallies = this.tallies;
    this.tallies = new Map();
    this.windowStart = null;

    const windowStart = new Date(start).toISOString();
    const windowEnd = new Date(end).toISOString();
    const entries: Array<Record<string, unknown>> = [];
    for (const [flagKey, tally] of tallies) {
      let count = tally.count;
      let enabled = tally.enabled;
      while (count > 0) {
        const part = Math.min(count, MAX_ENTRY_COUNT);
        const partEnabled = Math.min(enabled, part);
        entries.push({
          flag_key: flagKey,
          count: part,
          enabled_count: partEnabled,
          window_start: windowStart,
          window_end: windowEnd,
        });
        count -= part;
        enabled -= partEnabled;
      }
    }
    for (let i = 0; i < entries.length; i += MAX_REPORT_ENTRIES) {
      const body = { evaluations: entries.slice(i, i + MAX_REPORT_ENTRIES) };
      try {
        const response = await this.transport.request(EVALUATIONS_PATH, { method: 'POST', body });
        if (!response.ok) {
          this.transport.report(await this.transport.httpError(response, EVALUATIONS_PATH, 'POST'), 'flush');
        }
      } catch (err) {
        this.transport.report(asExperimentationError(err), 'flush');
      }
    }
  }
}
