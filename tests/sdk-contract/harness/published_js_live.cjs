// Harness: a PUBLISHED @controlvar/js-sdk's public client in local mode, against a
// running backend (tests/sdk-contract/live/run_live_contract.py runs it and does
// every comparison; this only reports what the client answered).
//
//   node published_js_live.cjs <package dir> <config JSON>
//
// <package dir> is the verified extraction's `package` directory; the client is
// required by absolute path from it, from an empty working directory. The config
// names the server, the sdk:ruleset key, the flags, the user, the refresh interval
// and how long to wait for a change.
//
// It prints one JSON line once the client has loaded the ruleset and answered
// every flag ("loaded"), then waits for one line on stdin (the runner has turned
// the flags off through the API), then evaluates every flag until each answers
// "inactive" locally or the wait is over, and prints a second line ("after").
'use strict';

const path = require('path');
const fs = require('fs');
const readline = require('readline');

function nextLine() {
  return new Promise(resolve => {
    const lines = readline.createInterface({ input: process.stdin });
    lines.once('line', line => {
      lines.close();
      resolve(line);
    });
  });
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const [root, configText] = process.argv.slice(2);
  const config = JSON.parse(configText);
  const localPath = path.join(root, 'dist', 'local.js');
  const sdk = require(path.join(root, 'dist', 'index.js'));
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));

  const errors = [];
  const client = new sdk.ExperimentationClient({
    apiUrl: config.api_url,
    apiKey: config.api_key,
    evaluation: 'local',
    refreshIntervalMs: config.refresh_seconds * 1000,
    onError: (error, operation) => errors.push(`${operation}: ${error && error.message}`),
  });

  const answers = async () => {
    const out = {};
    for (const flag of config.flags) {
      const result = await client.evaluateFlag(flag, { userId: config.user_id });
      out[flag] = { enabled: result.enabled, reason: result.reason, source: result.source };
    }
    return out;
  };
  const statusOf = () => {
    const status = client.status();
    return { ready: status.ready, version: status.rulesetVersion };
  };
  const print = report => process.stdout.write(`${JSON.stringify(report)}\n`);

  const ready = await client.ready({ timeoutMs: config.ready_seconds * 1000 });
  const modules = Object.keys(require.cache).filter(file => file !== __filename);
  print({
    phase: 'loaded',
    identity: {
      package: pkg.name,
      version: pkg.version,
      local_module: require.resolve(localPath),
      modules,
      node: process.version,
    },
    ready: ready.ok ? { ok: true } : { ok: false, error: ready.error && ready.error.message },
    status: statusOf(),
    answers: await answers(),
    errors: errors.slice(),
  });

  await nextLine();
  const started = Date.now();
  let after = await answers();
  const off = a => Object.values(a).every(x => !x.enabled && x.reason === 'inactive' && x.source === 'local');
  while (!off(after) && Date.now() - started < config.wait_seconds * 1000) {
    await sleep(250);
    after = await answers();
  }
  print({
    phase: 'after',
    seconds: (Date.now() - started) / 1000,
    status: statusOf(),
    answers: after,
    errors: errors.slice(),
  });
  await client.close();
}

main().catch(err => {
  process.stderr.write(`${err && err.stack ? err.stack : err}\n`);
  process.exit(1);
});
