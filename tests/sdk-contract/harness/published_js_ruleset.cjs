// Harness: a PUBLISHED @controlvar/js-sdk's own local evaluator, fed the
// ruleset the server sends (tests/sdk-contract/published_sdks.py runs it and
// does every comparison; this only reports what the evaluator did).
//
//   node published_js_ruleset.cjs <package dir> <config JSON>
//
// <package dir> is the verified extraction's `package` directory; the modules
// are required by absolute path from it, and the harness runs from an empty
// working directory. The config names the vectors file and the sentinel flag.
//
// The evaluator's LocalEvaluation runtime is driven through three documents
// by a stub transport (no network): a baseline holding only the sentinel, on;
// the vectors' `ruleset` plus the sentinel, on; the same with the sentinel off.
// After each, the runtime's status and its answer for the sentinel are
// reported. Every case is then answered with the package's own indexRuleset
// and evaluateLocally over the second document, exactly as the SDK's own
// evaluator tests do. Prints one JSON report.
'use strict';

const fs = require('fs');
const path = require('path');

async function main() {
  const [root, configText] = process.argv.slice(2);
  const config = JSON.parse(configText);
  const localPath = path.join(root, 'dist', 'local.js');
  const evaluatorPath = path.join(root, 'dist', 'evaluator.js');
  const local = require(localPath);
  const evaluator = require(evaluatorPath);
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));

  // The vectors file's own text, parsed here: the numbers are the server's.
  const vectors = () => JSON.parse(fs.readFileSync(config.vectors, 'utf8'));
  const sentinelOn = {
    key: config.sentinel,
    active: true,
    evaluation: 'local',
    rollout_percentage: 100,
    rules: [],
    default_rule: null,
  };
  const sentinelOff = { key: config.sentinel, active: false };
  const withSentinel = (flag, version) => {
    const ruleset = vectors().ruleset;
    const doc = Object.assign({}, ruleset, { flags: ruleset.flags.concat([flag]) });
    if (version !== undefined) doc.version = version;
    return doc;
  };
  const documents = [
    [
      'baseline',
      {
        schema: evaluator.RULESET_SCHEMA,
        version: config.baseline_version,
        bucketing: evaluator.BUCKETING,
        flags: [sentinelOn],
      },
    ],
    ['server', withSentinel(sentinelOn)],
    ['sentinel-off', withSentinel(sentinelOff, config.off_version)],
  ];

  let current = null;
  const reports = [];
  const sent = [];
  const transport = {
    async request(requestPath, options) {
      sent.push({ path: requestPath, method: options.method, headers: options.headers || {} });
      if (requestPath === local.RULESET_PATH && options.method === 'GET') {
        const body = current;
        // No ETag: the runtime then keys its next request on the version.
        return { status: 200, ok: true, headers: { get: () => null }, json: async () => body };
      }
      return { status: 202, ok: true, headers: { get: () => null }, json: async () => ({}) };
    },
    report(error, operation) {
      reports.push({ operation, message: error && error.message });
    },
    async httpError(response, requestPath, method) {
      return new Error(`${method} ${requestPath} failed with HTTP ${response.status}`);
    },
  };

  const runtime = new local.LocalEvaluation(transport, { refreshIntervalMs: 30000 });
  const shown = answer => (answer === evaluator.DEFER ? 'DEFER' : { enabled: answer.enabled, reason: answer.reason });
  const steps = [];
  for (const [label, doc] of documents) {
    current = doc;
    await runtime.refresh();
    const status = runtime.status();
    steps.push({
      label,
      version: status.rulesetVersion,
      ready: status.ready,
      error: status.lastError ? status.lastError.message : null,
      sentinel: shown(runtime.answer(config.sentinel, config.probe_user, undefined)),
    });
  }

  // Which flags of the server's document the evaluator cannot read on their own.
  const server = documents[1][1];
  const refused = [];
  for (const flag of server.flags) {
    const alone = {
      schema: evaluator.RULESET_SCHEMA,
      version: 'one-flag',
      bucketing: evaluator.BUCKETING,
      flags: [flag],
    };
    if (!evaluator.indexRuleset(alone)) {
      refused.push(flag && typeof flag === 'object' && 'key' in flag ? String(flag.key) : JSON.stringify(flag));
    }
  }

  const all = vectors();
  const indexed = evaluator.indexRuleset(server);
  let answers = null;
  let indexError = null;
  if (indexed) {
    answers = all.cases.map(c => shown(evaluator.evaluateLocally(indexed, c.flag, c.user_id, c.context)));
  } else {
    indexError = evaluator.isSupportedFormat(server)
      ? 'indexRuleset refused the document'
      : `a format this SDK does not understand (schema ${JSON.stringify(server.schema)}, bucketing ${JSON.stringify(server.bucketing)})`;
  }

  const modules = Object.keys(require.cache).filter(file => file !== __filename);
  process.stdout.write(
    JSON.stringify({
      identity: {
        package: pkg.name,
        version: pkg.version,
        local_module: require.resolve(localPath),
        modules,
        node: process.version,
      },
      steps,
      refused_flags: refused,
      answers,
      index_error: indexError,
      reports,
      requests: sent.length,
    }),
  );
}

main().catch(err => {
  process.stderr.write(`${err && err.stack ? err.stack : err}\n`);
  process.exit(1);
});
