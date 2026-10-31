# SDK contract tests

Three layers keep the SDKs honest: the golden vectors and the ruleset vectors
(offline, against the SDKs in this tree), the published local evaluators
(against the releases on npm and PyPI), and the live contract (against a
running backend).

## 1. Golden vectors (offline)

`golden-vectors.json` fixes the MD5 consistent-hash function every SDK exports
(`{userId}:{flagKey}` → first 4 bytes little-endian ÷ 2^32). `hash_contract.py` runs each SDK's
**own** exported function against it -- not a copy of the algorithm -- through a thin harness per
SDK in `harness/`, and does every comparison itself: the hash (within 1e-10, less than one bucket),
the MD5 digest where the SDK exports one, the rollout inclusions, and exactly 9 hash vectors and
2 rollout vectors answered by each SDK.

| SDK | What runs | Needs |
| --- | --- | --- |
| `python` | `controlvar.consistent_hash` / `md5_hex` from `sdk/python` | python |
| `js` | `consistentHash` / `md5Hex` from the built package (`npm ci && npm run build` in `sdk/js`) | node |
| `edge` | `hashUser` / `md5Hex` from the built package (`sdk/edge`) | node |
| `react-native` | `hashUser` from `src/hash.ts`, loaded with node's type stripping (the package ships source) | node 22 |
| `go` | `ConsistentHash`, compiled from `sdk/go` through a `replace` directive | go |

`--list` names every other `sdk/` directory and why this job does not run it (iOS needs
CommonCrypto, Android a Kotlin build; their own hash tests run in `sdk-unit-tests.yml`), and fails
on a directory it does not classify. These run in the `SDK Contract Tests` job of the PR gate, one
step per SDK, and need no backend. The pytest run is the comparison's own tests.

```bash
python tests/sdk-contract/hash_contract.py --list
python -m pytest tests/sdk-contract -q -o addopts=""
python tests/sdk-contract/hash_contract.py python js edge react-native go
```

The hash is a utility only, and it is not the function the server buckets flag rollouts with. Since the
September 2026 SDK rewiring assignment and flag evaluation are decided by the backend; the one exception is
the opt-in, server-side local evaluation of the JavaScript and Python SDKs (below), which reproduces the
server's own `md5-mod100-v1` flag bucketing, not this hash.

## Ruleset vectors (server-side local evaluation, #226)

`ruleset-vectors.json` is the differential corpus for SDKs that evaluate flags locally from
`GET /api/v1/sdk/ruleset` (beta). It is **generated from the server**, never written by hand:
`backend/scripts/generate_ruleset_vectors.py` builds each corpus flag's ruleset entry with the
server's ruleset builder and runs the server's own flag evaluator for every case. The first
command rewrites the file; the second exits 1 if it is stale.

```bash
python -m backend.scripts.generate_ruleset_vectors
python -m backend.scripts.generate_ruleset_vectors --check
```

What it holds:

- `ruleset`: a complete ruleset document, as the endpoint serves it, for the corpus flags;
- `local_operators`: the one list of operators an SDK may evaluate itself (every other operator,
  and the legacy list shape, makes a flag `"evaluation": "remote"`); each SDK checks its own list
  against this one;
- `max_safe_integer` and `whitespace_code_points`: the numeric and whitespace limits of the local
  domain (a number of greater magnitude, or `is_null` on a string containing one of those code
  points, is answered by the server);
- `cases`: `(flag, user_id, context) → expected {enabled, reason}`, each marked `must_local` or
  not, and `counts`.

The property an SDK must show: for **every** case it either defers to the server or returns
exactly `expected`, and for every `must_local` case it answers locally with exactly `expected`.
`counts.must_local` is the number of such cases, so an SDK that defers everything fails.

Which SDKs run it: the JavaScript SDK (`sdk/js/__tests__/evaluator.test.ts` runs its evaluator over
every case and pins the local count, the operator list and the whitespace list against this file;
`sdk/js/__tests__/local.test.ts` runs the cases through the public client), and the Python SDK
(`sdk/python/tests/test_evaluator.py` and `test_local.py`, the same checks). They run in the `js` and
`python` jobs of `SDK Unit Tests`, which every change under `tests/sdk-contract/` triggers.

Contexts are listed in their key order, which matters: the server flattens nested objects
first-writer-wins. No context relies on the order of integer-like keys, which JavaScript objects
reorder.

`backend/tests/unit/services/test_sdk_ruleset_vectors.py` fails when the file is stale (it
regenerates it in-process, with no database and no `.git`), pins the counts and the operator list,
and runs a reference local evaluator over the file. A change to the rules engine, the targeting
adapter or the flag service that moves any answer fails there until the file is regenerated.

## 2. Published local evaluators (#1497)

The SDKs in this tree are not the only ones reading the ruleset. Every release already on a
registry with a local evaluator keeps reading what the server sends, and a document it cannot read
is dropped whole: it keeps answering from its last ruleset, so a later kill switch never reaches
it, and nothing on the server says so. `published-sdks.json` pins each such release by URL and
digest (npm `@controlvar/js-sdk` and PyPI `controlvar`, 0.1.1 and 0.2.0), with a declared
`expectation`: `accepted` for all four.

`test_published_ruleset.py` (G1) downloads each one (`published_sdks.py`: three attempts, five
seconds apart), checks the digest before unpacking it and on every later load, refuses an archive
with an absolute path, a `..` or a link in it, and unpacks only the evaluator's code into a fresh
directory. A harness (`harness/published_js_ruleset.cjs` under node, required by absolute path;
`harness/published_python_ruleset.py` under `python -I -S` with only the extraction on
`sys.path`) runs it in its own process from an empty working directory, and the test requires:

- identity: every SDK module it loaded is inside the verified extraction;
- outcome: driven through a baseline document, then `ruleset-vectors.json`'s `ruleset` plus a
  sentinel flag, then the sentinel turned off, it loads the server's ruleset (its version moves)
  and the sentinel answers `{enabled: false, reason: "inactive"}`. A release that refuses the
  format passes only if its pin says `format-refused`, which is a decision taken in that file;
- answers: every case is DEFER or exactly the server's `{enabled, reason}`, and the cases it
  answers locally are exactly the `must_local` ones (1294 today; the count is logged).

Every run also plants a defect into the inputs, against the same four releases, and checks the
failure by its text: P1 an unknown `evaluation` (fails, naming the flag), P2 a rule with no
integer percentage, P3 a percentage of `50.0` (fails for Python, passes for JavaScript, whose
`JSON.parse` makes it 50), P4 schema 2 under `accepted` (fails; passes under `format-refused`),
P5 keys the releases do not know at every level (passes), P6 the same files loaded from outside
the verified extraction (fails on identity), P7 one expected answer flipped, P8 a wrong digest
(fails before anything is unpacked).

`backend/tests/integration/api/test_ruleset_published_sdk_compat.py` (G2) does the same for the
two Python releases with the route's own response bytes: a DEVELOPER creates flags of every shape
through the API, the releases load `GET /api/v1/sdk/ruleset`, answer exactly as `/evaluate`
does, and both kill switches (deactivate, and a safety rollback to 0%) reach them.

There is no skip. With no network or no node the tests fail and say so. They run wherever
`pytest tests/sdk-contract` runs: the SDK Contract Tests job, core-build's `vectors` step,
nightly-qa, and rc-evidence at a release tag.

`published_versions_check.py` lists every version on both registries, reads each file's member
names (no extraction, no import), and fails when a release with a local evaluator is not pinned,
or a pin is not the registry's file and digest. It runs in nightly-qa and rc-evidence, not on pull
requests: a publish would otherwise turn every open pull request red until the pin lands. A new
release with a local evaluator is added to `published-sdks.json` after it is published.

```bash
python -m pytest tests/sdk-contract/test_published_ruleset.py -o addopts="" -q
python tests/sdk-contract/published_versions_check.py
```

## 3. Live contract (against a running backend)

`live/run_live_contract.py` runs every SDK's `contract_smoke` entry point against a real backend
and checks the JSON it prints: a sticky assignment (`control` / `treatment`), a flag evaluation
(`enabled: true`), one tracked event with an experiment key, and one key-less event that fans out
to the cached assignment and flag. Before the SDKs it checks `GET /api/v1/sdk/ruleset` once: the
seed's plain key gets 403, its `sdk:ruleset` key gets the ruleset with `sdk_contract_flag` in it,
and sending the ETag back gets 304 (the `ruleset (server)` row; `CONTROLVAR_LOCAL_API_KEY`
overrides the scoped key). After the SDKs, each SDK with a local mode runs its local smoke with that
key (the `python (local)` and `js (local)` rows): the runner reads the database before and after, through the backend's own
`SafetyService.get_error_metrics`, and requires no new per-user evaluation row (no evaluate call),
the safety denominator grown by exactly the local evaluations, and the counted error count by
exactly the errors the smoke reported with the `sdk:ruleset` key. It then reports one error with the
seed's plain key and requires that it is stored but not counted (`error_count` +0,
`uncounted_error_count` +1): only reports sent with a server-side key count toward automatic
rollback. The runner therefore needs the backend's Python environment and `POSTGRES_*`.

```bash
source venv/bin/activate
export APP_ENV=development POSTGRES_SERVER=localhost POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation

python backend/scripts/seed_sdk_contract.py
uvicorn backend.app.main:app --port 8000 &
python tests/sdk-contract/live/run_live_contract.py
python tests/sdk-contract/live/run_live_contract.py --sdk go --sdk python --strict
```

- `python backend/scripts/seed_sdk_contract.py`: experiment sdk_contract_ab, flag sdk_contract_flag, API keys -> live/.api_key, live/.api_key_local (sdk:ruleset); runs only with ENVIRONMENT development or test, like the demo seeds
- `uvicorn backend.app.main:app --port 8000 &`: or ./demo/setup-local.sh
- `python tests/sdk-contract/live/run_live_contract.py`: all SDKs whose toolchain is installed

Each SDK documents its smoke command in its README; the runner's `MANIFEST` lists them. Missing
toolchains are skipped (reported as `SKIP`) unless `--strict` is given. The seeded API key can be
overridden with `CONTROLVAR_API_KEY`, the backend with `CONTROLVAR_API_URL` (the runner still reads
the former names, `EXPERIMENTLY_API_KEY` and `EXPERIMENTLY_API_URL`, when these are not set).

Three entries need no toolchain beyond node or a JVM, but get there differently from the rest:

| SDK | How it runs under the contract |
| --- | --- |
| `react` | `sdk/react/examples/contract_smoke.mjs` drives the SSR entry point (`ServerClient`) plus `ExperimentationClient` for tracking, under plain node — no DOM, no React render. `trackEvent` never throws, so the smoke wraps `fetch` and fails on any tracking call that did not return 2xx. |
| `react-native` | A Jest test (`sdk/react-native/examples/contract_smoke.test.ts`, `jest.contract.config.js`) under `testEnvironment: node` with the in-memory AsyncStorage mock — no device, emulator or Metro. The package ships no build output, so Jest's ts-jest transform is the build. Jest owns stdout, so the report is written to `.contract_smoke.json` and the MANIFEST command `cat`s it. |
| `android` | `sdk/android/jvm/` compiles the Android SDK's Kotlin sources for a plain JVM (they touch no `android.*` API) with Maven, and `sdk/android/examples/contract_smoke.sh` runs the smoke from that build — no Android SDK, no emulator. The same module runs the SDK's 78 unit tests: `cd sdk/android/jvm && ./mvnw clean test`. |

Contract details (endpoints, bodies, fan-out rule, smoke output format) live in
`docs/sdk-guide.md` ("Endpoint contract").

**The fan-out rule includes held-out answers.** An assignment the server answered with
`"assigned": false` (the global holdout, a mutual exclusion group or targeting turned the user
away) is cached like any other, and a key-less `track()` fans out to it. Holdout results
(`GET /api/v1/holdout/{id}/results`, #445) depend on this: they count a held-out user's events
exactly as everyone else's, so an SDK that dropped `assigned: false` answers from the fan-out would
leave the holdout group without events and report a difference that is not there. The JavaScript
SDK pins it (`fans out to an assignment the global holdout answered`, in
`sdk/js/__tests__/client.test.ts`).
