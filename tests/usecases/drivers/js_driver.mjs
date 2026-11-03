/**
 * The JavaScript SDK's driver: this checkout's built sdk/js (sdk/js/dist) behind
 * newline-delimited JSON on stdin and stdout, spoken to by usecases/runner.py.
 * The same protocol as drivers/python_driver.py.
 *
 * One process holds one client. Each line in is one command; each line out is its
 * answer, one JSON object with `ok` true or false, in order:
 *
 * - `hello`: `sdk`, `version` (sdk/js/package.json's), `module` (the file the SDK
 *   was loaded from, relative to the repository root) and `capabilities`
 *   (`local-eval`, `remote-eval`, `error-tally`).
 * - `init {api_url, api_key, mode, refresh_seconds?, cache_ttl_seconds?,
 *   timeout_ms?}`: the client, `mode` `server` or `local`. In local mode it waits
 *   up to `timeout_ms` (5,000 unless given) for the first ruleset and answers
 *   `ready`. `cache_ttl_seconds` is passed as it is (0 caches nothing). The client
 *   always gets an `onError` that counts its calls per operation: the count only,
 *   never the error or its message, which can carry a URL with a user's id.
 * - `evaluate {flag, user, context?, default}`: `enabled`, `value`, `reason`,
 *   `source`, `error` and `ms`. `source` is `local` (from the ruleset), `server`
 *   (the SDK's server path; in server mode the SDK does not say whether its cache
 *   answered, so the proxy's count is the authority) or `default` (the call threw:
 *   `enabled` and `value` are the default and `error` names the error's type and
 *   status). `error` is `null` otherwise; it is never left out.
 * - `status`: the SDK's `status()`, and `errors`: the `onError` calls so far,
 *   `{refresh, flush, evaluate, other}` (any other operation counts as `other`).
 * - `close`: closes the client, answers, and exits 0. End of input does the same
 *   without an answer.
 *
 * Refused but answered: `unknown_command`, `not_initialised` (`evaluate` or
 * `status` before `init`), `already_initialised` and `bad_request` (a line that is
 * not a JSON object, or arguments of the wrong type).
 *
 * - Identity. The SDK is loaded from sdk/js/dist by its path from this file, and
 *   the driver refuses to start (exit 3, before reading any command) when the file
 *   Node loaded is not inside this checkout's sdk/js/dist once symbolic links are
 *   resolved. It refuses Node below 18, the SDK's engines floor (exit 4).
 * - The API key arrives in `init` and is never written back: answers are built from
 *   named fields, and an error carries the error's type and status, never its
 *   message.
 * - stdout carries only answers. Every console method writes to stderr, so nothing
 *   the SDK or Node prints can be read as an answer.
 *
 * Node's standard library and the SDK only.
 */

import { readFileSync, realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';
import { format } from 'node:util';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, '..', '..', '..');
const SDK_ROOT = path.join(REPO_ROOT, 'sdk', 'js');
const SDK_DIST = path.join(SDK_ROOT, 'dist');
/** The longest command line read. */
const MAX_LINE_BYTES = 64 * 1024;
/** Local mode: how long `init` waits for the first ruleset unless told. */
const DEFAULT_READY_MS = 5000;
/** Exit status when the SDK is not this checkout's. */
const NOT_THE_CHECKOUT = 3;
/** Exit status on a Node below the SDK's engines floor. */
const NODE_TOO_OLD = 4;
const NODE_FLOOR = 18;
/** The operations `errors` counts by name; any other counts as `other`. */
const ERROR_OPERATIONS = ['refresh', 'flush', 'evaluate'];
const CAPABILITIES = ['local-eval', 'remote-eval', 'error-tally'];

/** The client's `onError`: counts its calls per operation, never keeping the error. */
function errorTally() {
  const counts = { refresh: 0, flush: 0, evaluate: 0, other: 0 };
  const onError = (_error, operation) => {
    counts[ERROR_OPERATIONS.includes(operation) ? operation : 'other'] += 1;
  };
  return { counts, onError };
}

// stdout carries only answers: every console method goes to stderr.
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'trace']) {
  console[method] = (...args) => process.stderr.write(`${format(...args)}\n`);
}

function refuse(message, status) {
  process.stderr.write(`${message}\n`);
  process.exit(status);
}

const major = Number(process.versions.node.split('.')[0]);
if (!(major >= NODE_FLOOR)) {
  refuse(`Node ${process.versions.node} is below the JS SDK's floor of ${NODE_FLOOR}`, NODE_TOO_OLD);
}

/** The SDK from this checkout's sdk/js/dist; exits 3 when Node loads it from anywhere else. */
function loadSdk() {
  const require = createRequire(import.meta.url);
  let resolved;
  let dist;
  try {
    resolved = realpathSync(require.resolve(path.join(SDK_DIST, 'index.js')));
    dist = realpathSync(SDK_DIST);
  } catch {
    refuse("sdk/js/dist/index.js is missing: build this checkout's JS SDK first", NOT_THE_CHECKOUT);
  }
  if (!resolved.startsWith(dist + path.sep)) {
    refuse("the SDK was not loaded from this checkout's sdk/js/dist", NOT_THE_CHECKOUT);
  }
  const sdk = require(resolved);
  const version = JSON.parse(readFileSync(path.join(SDK_ROOT, 'package.json'), 'utf8')).version;
  return { sdk, version, module: path.relative(realpathSync(REPO_ROOT), resolved).split(path.sep).join('/') };
}

class Refused extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

/** An error as an answer may carry it: its type and status only. */
function errorOf(err) {
  const status = err && typeof err.status === 'number' ? err.status : null;
  const type = err && err.constructor && typeof err.constructor.name === 'string' ? err.constructor.name : 'Error';
  return { type, status };
}

function number(args, name) {
  const value = args[name];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Refused('bad_request');
  return value;
}

function string(args, name) {
  const value = args[name];
  if (typeof value !== 'string' || value.length === 0) throw new Refused('bad_request');
  return value;
}

class Driver {
  constructor(loaded) {
    this.loaded = loaded;
    this.client = null;
    this.errors = errorTally();
    this.closed = false;
  }

  async handle(command) {
    if (!command || typeof command !== 'object' || Array.isArray(command) || typeof command.cmd !== 'string') {
      return { ok: false, error: 'bad_request' };
    }
    const handler = Object.prototype.hasOwnProperty.call(Driver.COMMANDS, command.cmd)
      ? Driver.COMMANDS[command.cmd]
      : undefined;
    if (!handler) return { ok: false, error: 'unknown_command' };
    try {
      return { ok: true, ...(await this[handler](command)) };
    } catch (err) {
      if (err instanceof Refused) return { ok: false, error: err.code };
      throw err;
    }
  }

  async shutdown() {
    if (this.client !== null) {
      const client = this.client;
      this.client = null;
      await client.close();
    }
  }

  async hello() {
    return {
      sdk: 'js',
      version: this.loaded.version,
      module: this.loaded.module,
      capabilities: [...CAPABILITIES],
    };
  }

  async init(args) {
    if (this.client !== null) throw new Refused('already_initialised');
    const apiUrl = string(args, 'api_url');
    const apiKey = string(args, 'api_key');
    const mode = args.mode;
    if (mode !== 'server' && mode !== 'local') throw new Refused('bad_request');
    const config = { apiUrl, apiKey, evaluation: mode, onError: this.errors.onError };
    const timeoutMs = number(args, 'timeout_ms');
    if (timeoutMs !== undefined) config.timeoutMs = timeoutMs;
    const cacheTtl = number(args, 'cache_ttl_seconds');
    if (cacheTtl !== undefined) config.cacheTtlMs = cacheTtl * 1000;
    const refresh = number(args, 'refresh_seconds');
    if (refresh !== undefined) config.refreshIntervalMs = refresh * 1000;
    let client;
    try {
      client = new this.loaded.sdk.ExperimentationClient(config);
    } catch {
      throw new Refused('bad_request');
    }
    this.client = client;
    const answer = { mode };
    if (mode === 'local') {
      const result = await client.ready({ timeoutMs: timeoutMs ?? DEFAULT_READY_MS });
      answer.ready = Boolean(result && result.ok);
    }
    return answer;
  }

  async evaluate(args) {
    if (this.client === null) throw new Refused('not_initialised');
    const flag = string(args, 'flag');
    const user = string(args, 'user');
    const context = args.context;
    if (context !== undefined && context !== null && (typeof context !== 'object' || Array.isArray(context))) {
      throw new Refused('bad_request');
    }
    const fallback = args.default === undefined ? false : args.default;
    const started = performance.now();
    let evaluation;
    try {
      const subject = context ? { userId: user, attributes: context } : { userId: user };
      evaluation = await this.client.evaluateFlag(flag, subject);
    } catch (err) {
      return {
        enabled: fallback,
        value: fallback,
        reason: null,
        source: 'default',
        error: errorOf(err),
        ms: performance.now() - started,
      };
    }
    return {
      enabled: evaluation.enabled,
      value: evaluation.value === undefined ? null : evaluation.value,
      reason: evaluation.reason === undefined ? null : evaluation.reason,
      source: evaluation.source || 'server',
      error: null,
      ms: performance.now() - started,
    };
  }

  async status() {
    if (this.client === null) throw new Refused('not_initialised');
    const status = this.client.status();
    return {
      evaluation: status.evaluation,
      ready: status.ready,
      ruleset_version: status.rulesetVersion,
      last_refresh_at: status.lastRefreshAt ? status.lastRefreshAt.toISOString() : null,
      last_error: status.lastError ? errorOf(status.lastError) : null,
      server_evaluated_flags: [...status.serverEvaluatedFlags],
      errors: { ...this.errors.counts },
    };
  }

  async close() {
    await this.shutdown();
    this.closed = true;
    return {};
  }
}

Driver.COMMANDS = { hello: 'hello', init: 'init', evaluate: 'evaluate', status: 'status', close: 'close' };

function write(answer) {
  return new Promise(resolve => process.stdout.write(`${JSON.stringify(answer)}\n`, resolve));
}

async function main() {
  const driver = new Driver(loadSdk());
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of lines) {
    let answer;
    if (Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES) {
      answer = { ok: false, error: 'bad_request' };
    } else {
      let command;
      try {
        command = JSON.parse(line);
      } catch {
        command = undefined;
        answer = { ok: false, error: 'bad_request' };
      }
      if (answer === undefined) answer = await driver.handle(command);
    }
    await write(answer);
    if (driver.closed) break;
  }
  await driver.shutdown();
  process.exit(0);
}

main().catch(err => {
  // Nothing about the command or the key: only the error's type.
  process.stderr.write(`the JS driver failed: ${errorOf(err).type}\n`);
  process.exit(1);
});
