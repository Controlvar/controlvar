# Changelog

Notable changes to Experimently. Dates are release dates.

This file is written by hand, and is a carry-over from when this repository
was published by exporting a private one: release-please ran there, and its
commit links pointed at hashes this repository does not contain, because the
export rewrote history. Development moved here on 2026-09-25, so that no
longer applies and release-please can generate this file directly. Until it
does, entries below 0.2.2 are hand-written and the links in them are the
reason why.

## [0.28.1](https://github.com/getexperimently/experimently/compare/v0.28.0...v0.28.1) (2026-10-11)


### Bug Fixes

* **power:** the runtime estimate uses the unrounded users per variant per day ([#1122](https://github.com/getexperimently/experimently/issues/1122)) ([25c803d](https://github.com/getexperimently/experimently/commit/25c803d19a7f655d6714629ae763508ff89889a9))

## [0.28.0](https://github.com/getexperimently/experimently/compare/v0.27.0...v0.28.0) (2026-10-10)


### Features

* **infra:** the stacks no longer create the two DynamoDB tables nothing uses ([#1111](https://github.com/getexperimently/experimently/issues/1111)) ([45a579a](https://github.com/getexperimently/experimently/commit/45a579ace4761b9f0d948241b296ab040b5647fa))


### Bug Fixes

* **ai:** the sample-size answer's days to significance count every variant's users ([#1104](https://github.com/getexperimently/experimently/issues/1104)) ([3b913e4](https://github.com/getexperimently/experimently/commit/3b913e43dbd581855b094a7de2c7ccc03a9b4c09))
* **fuzz:** graphql-core goes back to 3.2.13, which hypothesis-graphql accepts ([#1108](https://github.com/getexperimently/experimently/issues/1108)) ([a5cec59](https://github.com/getexperimently/experimently/commit/a5cec5998e17cd1125ea4e6b8b7fb1105a1fa28c))
* **safety:** the automatic rollback alert carries the measured value and threshold ([#1105](https://github.com/getexperimently/experimently/issues/1105)) ([e3a53e0](https://github.com/getexperimently/experimently/commit/e3a53e00dc5ce42a4bffd3e3ab73200eb55fb881))


### Documentation

* the integration pages' setup and inbound webhook examples run as written ([#1101](https://github.com/getexperimently/experimently/issues/1101)) ([bf3ad9e](https://github.com/getexperimently/experimently/commit/bf3ad9e4a68a1cbf9b1ca0a06611307d64cee6c9))
* the migration and load-testing pages run as written ([#1097](https://github.com/getexperimently/experimently/issues/1097)) ([2599366](https://github.com/getexperimently/experimently/commit/25993661ddc3a38d9066cd229926c50872d0ee5c))

## [0.27.0](https://github.com/getexperimently/experimently/compare/v0.26.4...v0.27.0) (2026-10-08)


### Features

* **showcase:** capture the getting-started walkthrough from a local stack ([#1079](https://github.com/getexperimently/experimently/issues/1079)) ([6b4580d](https://github.com/getexperimently/experimently/commit/6b4580dfa52800c0d235a76f75f8a46a39bb9beb))
* **showcase:** render captured walkthroughs into captioned 1080p MP4s with subtitle files and a review page ([#1077](https://github.com/getexperimently/experimently/issues/1077)) ([d29a941](https://github.com/getexperimently/experimently/commit/d29a941cb600700f1bdb2dfed37884ff21aabe76))


### Bug Fixes

* **auth:** an http SSO URL is refused when an Okta configuration is saved ([#1095](https://github.com/getexperimently/experimently/issues/1095)) ([eb692fb](https://github.com/getexperimently/experimently/commit/eb692fb737c33534caa924ba030d389d4234c608))
* **dashboard:** the web container's access log leaves query strings out ([#1099](https://github.com/getexperimently/experimently/issues/1099)) ([7b73405](https://github.com/getexperimently/experimently/commit/7b7340530614a807cee3e4508523ac351c10a095))
* **logging:** the API log leaves sign-in and token query values out ([#1094](https://github.com/getexperimently/experimently/issues/1094)) ([7c377a0](https://github.com/getexperimently/experimently/commit/7c377a08bb8fa7c0a492ec19a35abf8c1dbb3ecb))


### Documentation

* the API reference, compliance, RBAC and technical guide pages run as written ([#1090](https://github.com/getexperimently/experimently/issues/1090)) ([1672943](https://github.com/getexperimently/experimently/commit/1672943b364eff1bc990c76046ac322b196cdd86))
* verify a release and describe the MCP manifest as they are, both run by Doc Examples ([#1084](https://github.com/getexperimently/experimently/issues/1084)) ([77e7594](https://github.com/getexperimently/experimently/commit/77e75943d6247d82b6ab55a071480c41b91c2219))

## [0.26.4](https://github.com/getexperimently/experimently/compare/v0.26.3...v0.26.4) (2026-10-07)


### Bug Fixes

* **dashboard:** a superuser's header fits on one line at 1280 px ([#1073](https://github.com/getexperimently/experimently/issues/1073)) ([979f3c8](https://github.com/getexperimently/experimently/commit/979f3c8b402ccdc37444f43ec0224f4b7dc83809))
* four small corrections (flag list 403 path, sample-size meter text, split-URL docs, a stale follow-up) ([#1062](https://github.com/getexperimently/experimently/issues/1062)) ([863c097](https://github.com/getexperimently/experimently/commit/863c097174c66b5f048281b7dae7eb69187cd76c))
* **rollouts:** stage_order is bounded, and an out-of-range value answers 422 ([#1081](https://github.com/getexperimently/experimently/issues/1081)) ([969493f](https://github.com/getexperimently/experimently/commit/969493f4795adc07c544e8d6c60faa59eb6a048a))
* **safety:** the automatic rollback reason rounds the measured value ([#1071](https://github.com/getexperimently/experimently/issues/1071)) ([c03e40e](https://github.com/getexperimently/experimently/commit/c03e40e07306435f8eeaf30a9b3d3593dc7eb4a3))


### Dependencies

* take the backend patch and minor updates except SQLAlchemy and FastAPI, which wait until after launch ([#1063](https://github.com/getexperimently/experimently/issues/1063)) ([126d696](https://github.com/getexperimently/experimently/commit/126d69666f3871ddbbf13c1225b8126027d41d6f))


### Documentation

* the inventory maps every AWS page to the step that exercises it, and checks workflow pages against their paths ([#1082](https://github.com/getexperimently/experimently/issues/1082)) ([429c4eb](https://github.com/getexperimently/experimently/commit/429c4ebd9f6097420e6aa399cc970a2c42a93d5b))

## [0.26.3](https://github.com/getexperimently/experimently/compare/v0.26.2...v0.26.3) (2026-10-07)


### Bug Fixes

* **dashboard:** roles pages say custom roles and grants do not yet change access, and the one-release results notice is gone ([#1058](https://github.com/getexperimently/experimently/issues/1058)) ([b504830](https://github.com/getexperimently/experimently/commit/b50483059c470eb4713d706cb264e6aabe6b0d3e))
* **dashboard:** the admin summary, workspace member counts and the rollout stage label show the right values ([#1055](https://github.com/getexperimently/experimently/issues/1055)) ([7cabb65](https://github.com/getexperimently/experimently/commit/7cabb65156a2ca5c58a9554dbc927e1c2f4b62b7))
* **infra:** the API error-log alarm counts lines whose JSON level is error or critical ([#1056](https://github.com/getexperimently/experimently/issues/1056)) ([4bcbeb7](https://github.com/getexperimently/experimently/commit/4bcbeb7b53f55e1052ad9c5926e2da47b8db1960))
* the split-URL docs, the LLM key settings and time-zone-dependent experiment dates ([#1061](https://github.com/getexperimently/experimently/issues/1061)) ([a0eb8a5](https://github.com/getexperimently/experimently/commit/a0eb8a5de70fa79657bd508d60e14a32a6989e21))


### Documentation

* correct statements that do not match the product (claims pass) ([#1059](https://github.com/getexperimently/experimently/issues/1059)) ([05dfd2f](https://github.com/getexperimently/experimently/commit/05dfd2f96258a3447ad9e95f4af75422803ca035))

## [0.26.2](https://github.com/getexperimently/experimently/compare/v0.26.1...v0.26.2) (2026-10-07)


### Bug Fixes

* **ai:** AI design and results interpretation stop after 30 seconds and have their own rate limit ([#1043](https://github.com/getexperimently/experimently/issues/1043)) ([5460961](https://github.com/getexperimently/experimently/commit/5460961b619820fc20ec568943ca5493c9757350))
* **api:** apply one request body size limit (5 MiB, 413) to every route ([#1052](https://github.com/getexperimently/experimently/issues/1052)) ([9ff5466](https://github.com/getexperimently/experimently/commit/9ff546619bf3ab09aaf90fc986f2177346e446c7))

## [0.26.1](https://github.com/getexperimently/experimently/compare/v0.26.0...v0.26.1) (2026-10-07)


### Bug Fixes

* **api:** an API-key request keeps its database connection after recording the key's use ([#1036](https://github.com/getexperimently/experimently/issues/1036)) ([013b8be](https://github.com/getexperimently/experimently/commit/013b8beb736c62381b9d4d47ae35d1c600360521))

## [0.26.0](https://github.com/getexperimently/experimently/compare/v0.25.4...v0.26.0) (2026-10-07)


### Features

* **runbooks:** a tested script points the application at a restored database ([#1017](https://github.com/getexperimently/experimently/issues/1017)) ([79ed116](https://github.com/getexperimently/experimently/commit/79ed116943956810421c9f52897efe69f78344ea))


### Bug Fixes

* **api:** health checks and the results cache stop holding the API when Redis is down ([#1023](https://github.com/getexperimently/experimently/issues/1023)) ([8dc3d68](https://github.com/getexperimently/experimently/commit/8dc3d68b21741feecd9c570c439877a73ba3e9f4))
* **dashboard:** grey text meets the contrast minimum, and the nightly browser suite stops hiding failures ([#1015](https://github.com/getexperimently/experimently/issues/1015)) ([b61fe43](https://github.com/getexperimently/experimently/commit/b61fe43d14d9358fe550246bc08cdc54d5549f08))
* **dashboard:** the power calculator's fields and figures have accessible names ([#1031](https://github.com/getexperimently/experimently/issues/1031)) ([e4ea52d](https://github.com/getexperimently/experimently/commit/e4ea52d15abfcaf2314f07feae8764d2ef49865a))
* **tracking:** concurrent first assignments for one user on different processes answer the stored assignment ([#1033](https://github.com/getexperimently/experimently/issues/1033)) ([91ccdbc](https://github.com/getexperimently/experimently/commit/91ccdbcd78ed763d542e07fc8bdb4617c8707451))


### Documentation

* **runbooks:** key rotation and the database restore step say what works ([#1012](https://github.com/getexperimently/experimently/issues/1012)) ([0ea49d4](https://github.com/getexperimently/experimently/commit/0ea49d42ffcf5f7a9aaef07ff34a560272f3642b))

## [0.25.4](https://github.com/getexperimently/experimently/compare/v0.25.3...v0.25.4) (2026-10-06)


### Bug Fixes

* **power:** planning advice is always the built-in advice ([#1011](https://github.com/getexperimently/experimently/issues/1011)) ([4c54916](https://github.com/getexperimently/experimently/commit/4c54916f5af162eb4103d197146d0700a9006e81))
* **tracking:** an event naming an id that does not exist answers 404, not 500 ([#1010](https://github.com/getexperimently/experimently/issues/1010)) ([7e78203](https://github.com/getexperimently/experimently/commit/7e782033d361e3ff2ba68708d0aaf7586ca87c06))

## [0.25.3](https://github.com/getexperimently/experimently/compare/v0.25.2...v0.25.3) (2026-10-06)


### Bug Fixes

* **ci:** the weekly load test seeds what it calls and records every request ([#992](https://github.com/getexperimently/experimently/issues/992)) ([1a2028a](https://github.com/getexperimently/experimently/commit/1a2028ae686b0d79c6ae04af6ff682422bc5f1e2))
* **power:** very large integers are refused with 422, not 500 ([#1005](https://github.com/getexperimently/experimently/issues/1005)) ([023fabf](https://github.com/getexperimently/experimently/commit/023fabfe14fa4068c169421ffd3089b5f4401196))
* **publish-guard:** drop the stale sample-ratio claim rule ([#999](https://github.com/getexperimently/experimently/issues/999)) ([18c4b95](https://github.com/getexperimently/experimently/commit/18c4b959d88fa822682605951b601cb6174cdb4a))


### Documentation

* **frontend:** the dashboard dev server is on port 3100 ([#998](https://github.com/getexperimently/experimently/issues/998)) ([e59e058](https://github.com/getexperimently/experimently/commit/e59e058d6e7bd9ada5b1963bfa100b33b244b129))
* **infrastructure:** the network page describes the CDK stack that exists ([#997](https://github.com/getexperimently/experimently/issues/997)) ([5e97304](https://github.com/getexperimently/experimently/commit/5e97304bc1c7a075f86e619c94addeb6db849e0c))
* **integrations:** the GitHub, Salesforce and FAQ pages name the integration by type and ADMIN-only changes ([#994](https://github.com/getexperimently/experimently/issues/994)) ([6fbdbcd](https://github.com/getexperimently/experimently/commit/6fbdbcd1bc85cab23968372030a9a25c2eb9c304))

## [0.25.2](https://github.com/getexperimently/experimently/compare/v0.25.1...v0.25.2) (2026-10-06)


### Bug Fixes

* **ci:** the load-test runner fails when it measured nothing ([#981](https://github.com/getexperimently/experimently/issues/981)) ([be3e83c](https://github.com/getexperimently/experimently/commit/be3e83ca016738b879c181b2a64c0a37cdbf390d))


### Dependencies

* sharp 0.35.5 in the dashboard and both demo apps ([#988](https://github.com/getexperimently/experimently/issues/988)) ([5e8f7a6](https://github.com/getexperimently/experimently/commit/5e8f7a6e5f99cc838076d4184283f5d3c7c0f1d1))


### Documentation

* **integrations:** the secret-rotation recipe and the roles table say what the API does ([#987](https://github.com/getexperimently/experimently/issues/987)) ([c645216](https://github.com/getexperimently/experimently/commit/c6452162632beb822e712661810fdad0e95dc86a))

## [0.25.1](https://github.com/getexperimently/experimently/compare/v0.25.0...v0.25.1) (2026-10-06)


### Bug Fixes

* **experiments:** an experiment started before its scheduled start can be completed ([#980](https://github.com/getexperimently/experimently/issues/980)) ([5471d82](https://github.com/getexperimently/experimently/commit/5471d82331d4915cc31d3ff68cb9c8712c93520f))
* **experiments:** starting a draft whose end date has passed answers a clear 4xx instead of 500 ([#972](https://github.com/getexperimently/experimently/issues/972)) ([1576f69](https://github.com/getexperimently/experimently/commit/1576f6910c7333b46a5cea9012636714e6cc6281))
* **safety:** a flag's safety config refuses a null enabled or metrics, and a stored null no longer breaks its reads ([#969](https://github.com/getexperimently/experimently/issues/969)) ([c6e0f08](https://github.com/getexperimently/experimently/commit/c6e0f081d82f1a858e111c86db62aa5d8b8a71d4))

## [0.25.0](https://github.com/getexperimently/experimently/compare/v0.24.0...v0.25.0) (2026-10-06)


### ⚠ BREAKING CHANGES

* **integrations:** integration responses no longer carry the values of stored secrets. `encrypted_config` carries only the connection settings, and `stored_secrets` names the other keys. `PUT /api/v1/integrations/{integration_type}` merges `encrypted_config` key by key instead of replacing it; a key sent as `null` is removed.

### Bug Fixes

* **integrations:** integration settings no longer return stored secrets ([#968](https://github.com/getexperimently/experimently/issues/968)) ([7dda975](https://github.com/getexperimently/experimently/commit/7dda97537abac2a4d713c11811c61c60f489dc1d))

## [0.24.0](https://github.com/getexperimently/experimently/compare/v0.23.2...v0.24.0) (2026-10-06)


### Features

* **experiments:** the experiment read names its owner ([#944](https://github.com/getexperimently/experimently/issues/944)) ([3ab4d8a](https://github.com/getexperimently/experimently/commit/3ab4d8a21c632e1ddcf355bc237dcb2907b30b39))


### Bug Fixes

* **users:** removing a user's account keeps the segments and rollout schedules it created ([#946](https://github.com/getexperimently/experimently/issues/946)) ([d905fea](https://github.com/getexperimently/experimently/commit/d905feaec630ffa31e6da293b026a3515aac1bb7))


### Documentation

* **quick-start:** the from-source dashboard runs on port 3100 ([#947](https://github.com/getexperimently/experimently/issues/947)) ([91e8d30](https://github.com/getexperimently/experimently/commit/91e8d30350907b56b47a1a7b2cf2ab6d405bab0b))

## [0.23.2](https://github.com/getexperimently/experimently/compare/v0.23.1...v0.23.2) (2026-10-06)


### Bug Fixes

* **dashboard:** a flag whose rules use NOT groups is shown as stored, with an accurate note ([#940](https://github.com/getexperimently/experimently/issues/940)) ([fe7cb52](https://github.com/getexperimently/experimently/commit/fe7cb522cf025793e3f85ddde1382fdbebe2b3cb))
* **dashboard:** flag screens offer create, toggle and save only to roles that may change flags ([#934](https://github.com/getexperimently/experimently/issues/934)) ([850923f](https://github.com/getexperimently/experimently/commit/850923f2894a182f604693231b3a283fb3443fac))
* **dashboard:** skip the sequential call when it is off, point non-superusers at the API-key route, article fix ([#930](https://github.com/getexperimently/experimently/issues/930)) ([4e0c51c](https://github.com/getexperimently/experimently/commit/4e0c51c6d7b97826e322c01ae6795a57ab15853d))
* **dashboard:** the header collapses to the menu button below 1280 px ([#938](https://github.com/getexperimently/experimently/issues/938)) ([1a2d787](https://github.com/getexperimently/experimently/commit/1a2d787b416476903af553ecc1b34e5c5ffc5cd1))
* **experiments:** an experiment whose creator's account was removed still reads and lists ([#935](https://github.com/getexperimently/experimently/issues/935)) ([cd1012b](https://github.com/getexperimently/experimently/commit/cd1012b664a7ab0435a6b7e86e799f4e450185d4))
* **results:** sequential analysis compares the control against the same treatment arm on every call ([#933](https://github.com/getexperimently/experimently/issues/933)) ([44ac4f4](https://github.com/getexperimently/experimently/commit/44ac4f4b7c7a9c51f20f58b00c48a54f230c31b3))


### Dependencies

* werkzeug 3.1.9 and source-map-js 1.2.2 in the shipped closures; a dated exception for python-jose in the test closure ([#932](https://github.com/getexperimently/experimently/issues/932)) ([ce3b7c8](https://github.com/getexperimently/experimently/commit/ce3b7c8939f490e0f656afb978e7a6614d3f8e3e))


### Documentation

* **bayesian:** the credible interval is equal-tailed ([#931](https://github.com/getexperimently/experimently/issues/931)) ([97e159a](https://github.com/getexperimently/experimently/commit/97e159ac486029d631629d56429a7aa471e3342e))

## [0.23.1](https://github.com/getexperimently/experimently/compare/v0.23.0...v0.23.1) (2026-10-05)


### Bug Fixes

* **dashboard:** the Audit Log page opens for every role the audit API serves ([#925](https://github.com/getexperimently/experimently/issues/925)) ([e4fcf3e](https://github.com/getexperimently/experimently/commit/e4fcf3e00aed41c735a524d3dda732c71ad23401))
* **results:** the results response carries the sequential block when sequential testing is enabled ([#928](https://github.com/getexperimently/experimently/issues/928)) ([4001db7](https://github.com/getexperimently/experimently/commit/4001db7e9e2f4bc3b702dcaac9414f1d1e478f58))


### Documentation

* **rbac:** the role tables say which actions need a superuser ([#924](https://github.com/getexperimently/experimently/issues/924)) ([242fd79](https://github.com/getexperimently/experimently/commit/242fd796c2292578c6ccffd29d725fb37403d444))

## [0.23.0](https://github.com/getexperimently/experimently/compare/v0.22.1...v0.23.0) (2026-10-05)


### Features

* **dashboard:** a page to change your own password ([#912](https://github.com/getexperimently/experimently/issues/912)) ([ba93aab](https://github.com/getexperimently/experimently/commit/ba93aabc42173d5ef414fab22c9ecb91e5fbfdb8))
* **warehouse:** BigQuery is available, after a passing check against a real account ([#906](https://github.com/getexperimently/experimently/issues/906)) ([bc977ff](https://github.com/getexperimently/experimently/commit/bc977ff6109986f0aa558411e514de7c19ed04f4))


### Bug Fixes

* **api:** the API reference pages render (/api/v1/docs, /api/v1/redoc) ([#911](https://github.com/getexperimently/experimently/issues/911)) ([b401db1](https://github.com/getexperimently/experimently/commit/b401db1314501546b7075333228fd511d6819bb3))
* **flags:** deleting a feature flag keeps its events ([#907](https://github.com/getexperimently/experimently/issues/907)) ([bcb6c3c](https://github.com/getexperimently/experimently/commit/bcb6c3c2a11f4d03d4e06e1c572ba8c3f0ad504c))
* **holdout:** holdout and exclusion-group admin routes accept ADMIN only, as documented ([#910](https://github.com/getexperimently/experimently/issues/910)) ([6cf1bc3](https://github.com/getexperimently/experimently/commit/6cf1bc35a1b9bf78f32bc8bb485d0047db726880))
* **results:** the sequential interval keeps its coverage when arms are split unequally ([#913](https://github.com/getexperimently/experimently/issues/913)) ([06d91a7](https://github.com/getexperimently/experimently/commit/06d91a71768603d4514b947e790af88695237939))
* **targeting:** a missing attribute makes only its own condition false on experiments ([#914](https://github.com/getexperimently/experimently/issues/914)) ([8512163](https://github.com/getexperimently/experimently/commit/851216319ea6afc44553f3eacc09673b7d174be0))

## [0.22.1](https://github.com/getexperimently/experimently/compare/v0.22.0...v0.22.1) (2026-10-05)


### Bug Fixes

* **warehouse:** BigQuery accepts the analysis query (AVG names its column) ([#901](https://github.com/getexperimently/experimently/issues/901)) ([1d9c557](https://github.com/getexperimently/experimently/commit/1d9c557ec529f731b4f90d057c48c85741fd5de4))

## [0.22.0](https://github.com/getexperimently/experimently/compare/v0.21.0...v0.22.0) (2026-10-05)


### Features

* **audit:** a Cognito sign-in is recorded ([#895](https://github.com/getexperimently/experimently/issues/895)) ([708d4e8](https://github.com/getexperimently/experimently/commit/708d4e836a8295b85c1e658bc8b645cc227dcbf6))
* **audit:** module changes are recorded: roles, workspace members, SSO sign-in and accounts ([#884](https://github.com/getexperimently/experimently/issues/884)) ([3c4fd48](https://github.com/getexperimently/experimently/commit/3c4fd486eb1a8770718ca00cb37a394a051e092c))
* **dashboard:** a bandit experiment's page shows its current traffic weights ([#885](https://github.com/getexperimently/experimently/issues/885)) ([7051cb7](https://github.com/getexperimently/experimently/commit/7051cb7611c5ac3de79198cabaf4675c6035d749))
* **dashboard:** a Segments page, CSV upload of user ids, and a segment picker in targeting rules ([#881](https://github.com/getexperimently/experimently/issues/881)) ([e92e43c](https://github.com/getexperimently/experimently/commit/e92e43c438738121d30e8a4889083643ef956094))
* **dashboard:** clone an experiment, and edit or delete a draft ([#890](https://github.com/getexperimently/experimently/issues/890)) ([c804bdc](https://github.com/getexperimently/experimently/commit/c804bdc31fceb14cb57a0c3240571934d62efa6d))
* **dashboard:** results show the sample-ratio check and the Bayesian analysis ([#887](https://github.com/getexperimently/experimently/issues/887)) ([54fcfb6](https://github.com/getexperimently/experimently/commit/54fcfb6e1d1d1f4a676f40b45ed3c492855b4a9b))
* **dashboard:** set each variant's configuration, and turn on Bayesian analysis, when creating an experiment ([#882](https://github.com/getexperimently/experimently/issues/882)) ([b057fa1](https://github.com/getexperimently/experimently/commit/b057fa117b1c54ecd1b0fba2c0759fd8083545f4))
* **deploy:** a rollback ends with one verdict line, chosen by a written precedence table ([#875](https://github.com/getexperimently/experimently/issues/875)) ([2988c8a](https://github.com/getexperimently/experimently/commit/2988c8a50ed9829ba88edbf707b3f118c0eed8d5))
* **holdout:** results for a holdout (beta) ([#866](https://github.com/getexperimently/experimently/issues/866)) ([a3f04eb](https://github.com/getexperimently/experimently/commit/a3f04eb313d3995800176fa618c483113a2b48c3))
* **interactions:** remove GET /interactions/{a}/{b}/novelty, which never computed a result; novelty detection is not offered ([ee1d2e1](https://github.com/getexperimently/experimently/commit/ee1d2e15e1e5f6ea4489d9fa855158056286ceb3))
* **interactions:** test whether two overlapping experiments' effects interact (beta) ([ee1d2e1](https://github.com/getexperimently/experimently/commit/ee1d2e15e1e5f6ea4489d9fa855158056286ceb3))
* **site:** the footer names who built it and links the repository ([#867](https://github.com/getexperimently/experimently/issues/867)) ([ed3e4aa](https://github.com/getexperimently/experimently/commit/ed3e4aa36f914035fe1cca1231bd4b5cb355c4e1))
* **targeting:** flags and experiments can target a segment ([#871](https://github.com/getexperimently/experimently/issues/871)) ([6d4c7c3](https://github.com/getexperimently/experimently/commit/6d4c7c34befafdd9c77596f6336a1403c9dc1d95))
* **warehouse:** Snowflake is available, after a passing check against a real account ([#877](https://github.com/getexperimently/experimently/issues/877)) ([290f0aa](https://github.com/getexperimently/experimently/commit/290f0aac2a11e47f9a7595f962b86302a1a45d50))


### Bug Fixes

* **results:** the recommendation is INCONCLUSIVE when the sample-ratio check fails ([#883](https://github.com/getexperimently/experimently/issues/883)) ([6c6ec1d](https://github.com/getexperimently/experimently/commit/6c6ec1dda29bba66c212445e693d1a8ebad55a16))
* **warehouse:** VIEWER no longer reads warehouse analyses or previews (D50) ([#886](https://github.com/getexperimently/experimently/issues/886)) ([0a5ba71](https://github.com/getexperimently/experimently/commit/0a5ba711df457d3ccd022c90d1293b59d6f1fdd7))


### Documentation

* **dashboard:** one table says what the dashboard does and what is API-only, and a check keeps it true ([#889](https://github.com/getexperimently/experimently/issues/889)) ([781c2d9](https://github.com/getexperimently/experimently/commit/781c2d9b1d6c857068ca6707fa2b1cd87739eab3))
* **guide:** ADMIN and DEVELOPER may schedule and delete any experiment ([#878](https://github.com/getexperimently/experimently/issues/878)) ([0c56351](https://github.com/getexperimently/experimently/commit/0c56351794958ef49a792408719797a32baa59cb))
* **rbac:** custom roles and direct grants are stored and shown; no permission check reads them yet ([#893](https://github.com/getexperimently/experimently/issues/893)) ([d7e4c03](https://github.com/getexperimently/experimently/commit/d7e4c036fc6cfb350a355cfb7110ff010f5c234b))
* **sdk:** the JavaScript SDK installs from npm ([#868](https://github.com/getexperimently/experimently/issues/868)) ([a900cce](https://github.com/getexperimently/experimently/commit/a900ccec4deecb2d2f3089d36e998f46791c546b))
* **sdk:** the OpenFeature provider's README says it is not on npm yet ([#879](https://github.com/getexperimently/experimently/issues/879)) ([02acfcc](https://github.com/getexperimently/experimently/commit/02acfcc293114f5d1e92bcf2282fc1978ba43c46))

## [0.21.0](https://github.com/getexperimently/experimently/compare/v0.20.0...v0.21.0) (2026-10-04)


### ⚠ BREAKING CHANGES

* **results:** `GET /results/{id}/cuped` uses each user's own events in the `covariate_lookback_days` (default 7) before their assignment as the covariate, instead of their position in the order of assignment, so `theta`, `variance_reduction_pct` and every `adjusted_*` value change for experiments set to `cuped` or `cuped_plus`. `metrics` has one entry per metric and treatment (`variant_id`, `variant_name`) instead of one for the first treatment; intervals use the experiment's `confidence_level`, and `corrected_p_value` applies its `correction_method`. A metric that cannot be computed is listed with `unavailable_reason` instead of being left out. `none` numbers are unchanged apart from the confidence level; `winsorization` on a conversion metric is listed with `winsorization_needs_mean_metric` instead of numbers that clipped every conversion to 0 below a 1% rate.

### Features

* **audit:** automatic changes record who made them: the schedulers, the safety monitor and Cognito sign-in ([#851](https://github.com/getexperimently/experimently/issues/851)) ([010d314](https://github.com/getexperimently/experimently/commit/010d314f39afdf305a25bf878edc2b61971748b9))
* **audit:** export the audit log as CSV or JSON, and label, filter and download it on the dashboard ([#861](https://github.com/getexperimently/experimently/issues/861)) ([c64ad44](https://github.com/getexperimently/experimently/commit/c64ad44e16073966c877c21db9ab3993bc0f55b2))
* **deploy:** api_serving.py --explain names why it answered as it did ([#862](https://github.com/getexperimently/experimently/issues/862)) ([23a92c6](https://github.com/getexperimently/experimently/commit/23a92c67ee953a4b7bf5aa997d5fa9da81b54616))
* **deploy:** the rollback's stop step records what it stopped and how its wait ended ([#864](https://github.com/getexperimently/experimently/issues/864)) ([45523d1](https://github.com/getexperimently/experimently/commit/45523d13dea4dfa90cf54022344e644c53b26d5c))
* **results:** CUPED adjusts for each user's own events before assignment ([#856](https://github.com/getexperimently/experimently/issues/856)) ([bb2f432](https://github.com/getexperimently/experimently/commit/bb2f4322bdbef82a14294cbd7739dbb197cd8928))
* **segments:** a segment can be a list of user ids, with routes to add and remove members ([#858](https://github.com/getexperimently/experimently/issues/858)) ([756885e](https://github.com/getexperimently/experimently/commit/756885e9de204cd46bc7fab2b1ba88d685fd9de4))


### Bug Fixes

* **interactions:** /scan answers 500 when its database reads fail ([#857](https://github.com/getexperimently/experimently/issues/857)) ([75dd8c1](https://github.com/getexperimently/experimently/commit/75dd8c1717c865fcadaa834cba9da97808c435ad))
* **warehouse:** read real Snowflake answers (Z offset, SHOW COLUMNS null?) ([#863](https://github.com/getexperimently/experimently/issues/863)) ([3d8a041](https://github.com/getexperimently/experimently/commit/3d8a041ba19210a3d73f1489b03d4b40d902c2b2))


### Documentation

* **sdk:** the Python SDK installs from PyPI ([#865](https://github.com/getexperimently/experimently/issues/865)) ([ba721e5](https://github.com/getexperimently/experimently/commit/ba721e5ca1bd12bb38a5bae042d8aae37784484d))

## [0.20.0](https://github.com/getexperimently/experimently/compare/v0.19.0...v0.20.0) (2026-10-04)


### ⚠ BREAKING CHANGES

* **holdout:** the stable `PUT /api/v1/holdout/{holdout_id}` now refuses, with 422, a `holdout_percentage` different from the stored one once the holdout has been active (the same value is still accepted). It also refuses, with 422, `is_active: true` on a holdout that has been deactivated. `POST /api/v1/holdout` with `is_active: true` now deactivates the holdout that was active. A concurrent activation that the database's one-active index refuses answers 409. Accepted pre-launch under D30 and D48, on the T80 precedent (T126).

### Features

* **audit:** every core route that changes something records it in the audit log ([#846](https://github.com/getexperimently/experimently/issues/846)) ([9ea0248](https://github.com/getexperimently/experimently/commit/9ea0248d34ec5a67662ad192b4896f6c9602e457))
* **holdout:** record who each holdout covers, and fix its percentage once it is active ([#844](https://github.com/getexperimently/experimently/issues/844)) ([1ea1874](https://github.com/getexperimently/experimently/commit/1ea1874a270d34bc3e3f3bae9368672e9fc337ad))
* **tracking:** accept events that name no experiment or flag, as pre-experiment history ([#850](https://github.com/getexperimently/experimently/issues/850)) ([a308702](https://github.com/getexperimently/experimently/commit/a3087026e6fd955b3399a839517418d625c72f4b))
* **tracking:** assign up to 1,000 users to an experiment in one call ([#849](https://github.com/getexperimently/experimently/issues/849)) ([1c76c0d](https://github.com/getexperimently/experimently/commit/1c76c0d785c3dbfa2dab8f6426a5e7af176f1158))

## [0.19.0](https://github.com/getexperimently/experimently/compare/v0.18.0...v0.19.0) (2026-10-04)


### ⚠ BREAKING CHANGES

* **experiments:** each experiment stores a `correction_method` and a `confidence_level`, and results for experiments with several treatments now use the stored correction: Benjamini-Hochberg at 0.95 unless an experiment is created with others. Existing experiments are backfilled to Benjamini-Hochberg at 0.95. `GET /results/{id}?correction_method=none` shows the earlier, uncorrected numbers. Both fields are locked once an experiment leaves draft, for every role. `adjusted_p_value` is now filled in (equal to `p_value`) for experiments with one treatment. The data export and the report follow the stored setting. CUPED and sequential testing do not follow the stored level.
* **segments:** segment rules must use the targeting rule format (`{"logical_operator", "groups": [{"conditions": [...]}]}`). The old `{"operator", "conditions"}` segment format is refused with 422 on `POST /api/v1/segments`, `PUT /api/v1/segments/{id}` and preview, and `POST /api/v1/segments/{id}/evaluate` answers 409 for a segment stored in that format (bulk-evaluate answers `false`). `{segment_id}` path parameters must be UUIDs.
* **workspaces:** the workspace API-key routes are removed and answer 404 (platform API keys are unchanged); workspace create and update refuse `plan` with 422, and workspace responses no longer carry plan limits or counts.

### Features

* **dashboard:** set and see each experiment's confidence level and correction ([#845](https://github.com/getexperimently/experimently/issues/845)) ([b7e4621](https://github.com/getexperimently/experimently/commit/b7e46216c299f0b56eaa398e4802b9b7db67a4c5))
* **experiments:** each experiment stores its correction method and confidence level (Benjamini-Hochberg, 95% by default) ([#843](https://github.com/getexperimently/experimently/issues/843)) ([f4bd9ea](https://github.com/getexperimently/experimently/commit/f4bd9eac5d929aac821311b2c8966de1265b8b57))
* **workspaces:** remove workspace API keys and plan limits ([#829](https://github.com/getexperimently/experimently/issues/829)) ([c3320b1](https://github.com/getexperimently/experimently/commit/c3320b18b22fd140ec6d192024f000a55cfb2196))


### Bug Fixes

* **audit:** flag status changes answer 200 and keep their audit entry when the audit write fails ([#841](https://github.com/getexperimently/experimently/issues/841)) ([2cc53d1](https://github.com/getexperimently/experimently/commit/2cc53d1257330a0d15cea019dd37b0216fbc15ee))
* **deploy:** the rollback verify step reads the active task set's own counts ([#833](https://github.com/getexperimently/experimently/issues/833)) ([da74042](https://github.com/getexperimently/experimently/commit/da74042442ca5450e287816c24bd168feee7cf33))
* **segments:** segment rules use the targeting rule format and are checked when saved ([#840](https://github.com/getexperimently/experimently/issues/840)) ([f86a004](https://github.com/getexperimently/experimently/commit/f86a004fdb861d1ae4a3924fa2d9c556fb5c6fee))
* **site:** the marketing homepage shows a contact address, and a privacy page exists ([#824](https://github.com/getexperimently/experimently/issues/824)) ([7a371d9](https://github.com/getexperimently/experimently/commit/7a371d92fa0e89e0d0a384519b80ef6d5a3c0fd9))


### Documentation

* **api:** the 429 text and the assign response match what the API sends ([#832](https://github.com/getexperimently/experimently/issues/832)) ([f852d43](https://github.com/getexperimently/experimently/commit/f852d43db966133b1f57f7ae93ea0420a56bf057))
* **demo:** the audit page claims only what it records ([#839](https://github.com/getexperimently/experimently/issues/839)) ([ee013fc](https://github.com/getexperimently/experimently/commit/ee013fc0210da31742352a2f13bafe755f8a5d24))
* **deploy:** the API's running counts are the PRIMARY task set's own ([#842](https://github.com/getexperimently/experimently/issues/842)) ([136e3de](https://github.com/getexperimently/experimently/commit/136e3deb5c1151080b9475a53515a9395df6ad0d))
* **holdout:** say what a global holdout does today ([#836](https://github.com/getexperimently/experimently/issues/836)) ([e21327f](https://github.com/getexperimently/experimently/commit/e21327f4ba47cd09861dd6f05a6772e084b53bd5))

## [0.18.0](https://github.com/getexperimently/experimently/compare/v0.17.0...v0.18.0) (2026-10-04)


### ⚠ BREAKING CHANGES

* **etl:** POST /api/v1/etl/partitions/add answers 500 when Glue refuses to read the table or register the partitions, 404 "Glue table not found" when the configured table is not in the catalog, and 422 for a date that is not YYYY-MM-DD; it previously answered 201.
* **api:** GET /api/v1/edge/bootstrap and GET /api/v1/openfeature/flags are removed and answer 404. Use GET /api/v1/sdk/ruleset (an API key with the sdk:ruleset scope) for server-side local evaluation, or GET /api/v1/feature-flags/evaluate/{flag_key}; experiments: POST /api/v1/tracking/assign.
* **api:** feature-flag targeting rules refuse unknown keys at every level ([#747](https://github.com/getexperimently/experimently/issues/747))

### Features

* **api:** remove the deprecated edge bootstrap and OpenFeature flag listings ([#770](https://github.com/getexperimently/experimently/issues/770)) ([ca9c05a](https://github.com/getexperimently/experimently/commit/ca9c05a66b2b95466c12d6f5d55f6a16da8a92ba))
* **deploy:** the API canary holds 10% of traffic for 15 minutes before the full shift ([#796](https://github.com/getexperimently/experimently/issues/796)) ([e7e85ad](https://github.com/getexperimently/experimently/commit/e7e85adbea687a94728f985b8b45a5a4e5b0a739))
* **deploy:** warn when the deployment group's config differs from the stack's ([#806](https://github.com/getexperimently/experimently/issues/806)) ([74b2461](https://github.com/getexperimently/experimently/commit/74b24616c2157234083803656370e7fc9b1bb544))


### Bug Fixes

* **api:** feature-flag targeting rules refuse unknown keys at every level ([#747](https://github.com/getexperimently/experimently/issues/747)) ([6480426](https://github.com/getexperimently/experimently/commit/6480426f4104b4ed021c4d10343097789f5065d3))
* **api:** the rate limiter goes back to Redis after an outage ([#813](https://github.com/getexperimently/experimently/issues/813)) ([134b108](https://github.com/getexperimently/experimently/commit/134b108439c9578b65efec72fe8b8e097ec2af8b))
* **api:** user fields longer than their columns answer 422 ([#753](https://github.com/getexperimently/experimently/issues/753)) ([1c746ae](https://github.com/getexperimently/experimently/commit/1c746aeb2730a83709ad11bf0d749379ead86aa7))
* **auth:** local sign-in matches the email address whatever its letter case ([#745](https://github.com/getexperimently/experimently/issues/745)) ([873529c](https://github.com/getexperimently/experimently/commit/873529c02cb194a4ea2718545d0a9aa87d9a2693))
* **ci:** the chart upgrade test falls back to the previous release while the newest one's images are publishing ([#762](https://github.com/getexperimently/experimently/issues/762)) ([4bfc5a2](https://github.com/getexperimently/experimently/commit/4bfc5a28d2bd8b1b37adbddf6a67207185b38b6b))
* **deploy:** a rollback's result reports what is serving when its own steps did not finish ([#766](https://github.com/getexperimently/experimently/issues/766)) ([2ed2e94](https://github.com/getexperimently/experimently/commit/2ed2e9464e5ae229f5577cdc8b4e434e9f079f9a))
* **deploy:** after stopping a deployment, the rollback waits for CodeDeploy's own revert before creating its own ([#797](https://github.com/getexperimently/experimently/issues/797)) ([2fe9da1](https://github.com/getexperimently/experimently/commit/2fe9da1e0e725d867bef4d9e586ed3893c320032))
* **deploy:** the CodeDeploy alarm override carries an alarm list ([#781](https://github.com/getexperimently/experimently/issues/781)) ([97343e7](https://github.com/getexperimently/experimently/commit/97343e70bd94b7112d5f51ccc004c57481afdbc8))
* **deploy:** the deploy policy grants the tagging that a pre-migration snapshot needs ([#746](https://github.com/getexperimently/experimently/issues/746)) ([8b835fa](https://github.com/getexperimently/experimently/commit/8b835fa19ff2911325b68c91bddd0b00b2228d7c))
* **deploy:** the migration workflow refuses relative and whole-graph targets ([#764](https://github.com/getexperimently/experimently/issues/764)) ([3b7655d](https://github.com/getexperimently/experimently/commit/3b7655d15b22e4d22b048308e479e0a56ef889a1))
* **deps:** build-only CSS tooling is a dev dependency, so the production audit reads what ships ([#757](https://github.com/getexperimently/experimently/issues/757)) ([d997866](https://github.com/getexperimently/experimently/commit/d99786662606184e7fac2a8a19bbefa4c62b1db1))
* **etl:** adding partitions reports a refused catalog write instead of answering 201 ([#769](https://github.com/getexperimently/experimently/issues/769)) ([7d565b8](https://github.com/getexperimently/experimently/commit/7d565b8ae64138891c7a363a4a2e7879af056b32))
* **infra:** the deploy policy allows the CodeDeploy revision lookup a rollback needs ([#756](https://github.com/getexperimently/experimently/issues/756)) ([5b48b76](https://github.com/getexperimently/experimently/commit/5b48b7656bc83292f7d256e1ccbaf8d20cbe3659))
* **infra:** the load balancer keeps its route to the API tasks when green is live ([76b14a8](https://github.com/getexperimently/experimently/commit/76b14a86a54138fa7e6d7f6e898f24c36d23cf0c))
* **sdk:** the OpenFeature provider publishes with an npm range for the JS SDK ([#778](https://github.com/getexperimently/experimently/issues/778)) ([998e84b](https://github.com/getexperimently/experimently/commit/998e84b88fa6e95abb6f4630acb2a2b414996f3b))
* **sdk:** the OpenFeature providers are versioned 0.1.0 like the SDKs they wrap ([#751](https://github.com/getexperimently/experimently/issues/751)) ([0789d9f](https://github.com/getexperimently/experimently/commit/0789d9f829c37e5569c90bf3f15eacd1c2a0217b))
* **site:** the homepage and docs hub describe what ships today ([#771](https://github.com/getexperimently/experimently/issues/771)) ([ce3ab0c](https://github.com/getexperimently/experimently/commit/ce3ab0c9586a45ccd5fac5cca68deca9fdd57c95))


### Documentation

* **auth:** the user guide describes how a password is actually reset ([#765](https://github.com/getexperimently/experimently/issues/765)) ([fd522c4](https://github.com/getexperimently/experimently/commit/fd522c459e45fe233c397f6069842619c7127bd8))
* **claude:** release, release-PR, rollback, mypy and URL notes match the repo ([#793](https://github.com/getexperimently/experimently/issues/793)) ([0d7a34c](https://github.com/getexperimently/experimently/commit/0d7a34c5a9258bc0209500b7858bd3e322abf3cd))
* **deploy:** preview a stack deploy with the same pins, and stop if it touches an ingress or egress rule ([#804](https://github.com/getexperimently/experimently/issues/804)) ([7779b17](https://github.com/getexperimently/experimently/commit/7779b172df1b27864c15d2b2499df5afb52fb712))
* **deploy:** secret rotation and the restore restart use CodeDeploy, which controls the API service ([#812](https://github.com/getexperimently/experimently/issues/812)) ([19ecfdb](https://github.com/getexperimently/experimently/commit/19ecfdbe564435ad9bd5664e7a5354a7fe42d887))
* **deploy:** undo a migration before rolling the API back, and say there is no supported downgrade after ([#763](https://github.com/getexperimently/experimently/issues/763)) ([f13a7f3](https://github.com/getexperimently/experimently/commit/f13a7f3198c3f8073ac61209dbcf9d2d599b2475))
* rollback and restore wording, the issue chooser link, and three statements match what runs ([#803](https://github.com/getexperimently/experimently/issues/803)) ([c75ec3a](https://github.com/getexperimently/experimently/commit/c75ec3a0a920b77589a2e2480719df89ffa0c683))
* SDK versions, CI coverage, DCO, the API start command and the warehouse routes match the code ([#791](https://github.com/getexperimently/experimently/issues/791)) ([3f98293](https://github.com/getexperimently/experimently/commit/3f98293624f121abb4aacd246b1337fc63c0e833))
* **self-hosting:** point the hostname at the load balancer after cdk deploy ([#752](https://github.com/getexperimently/experimently/issues/752)) ([fe38c48](https://github.com/getexperimently/experimently/commit/fe38c483c80d8d4c1c9206faa4344039110a69b4))

## [0.17.0](https://github.com/getexperimently/experimently/compare/v0.16.2...v0.17.0) (2026-10-02)


### ⚠ BREAKING CHANGES

* **api:** feature-flag targeting rules are validated when saved ([#743](https://github.com/getexperimently/experimently/issues/743))
* **flags:** a legacy targeting condition with an operator outside eq/ne/gt/lt/contains/in no longer matches ([#740](https://github.com/getexperimently/experimently/issues/740))
* **safety:** A safety rollback to 0% now turns the flag off for every user, including users matched by a targeting rule, and pauses the flag's rollout schedule. A rollback to 1–100% still lowers only the global rollout. This supersedes the #706 note that rule-matched users keep the flag.
* **events:** event times stored with a non-UTC offset are rewritten to the same instant in UTC (+00:00). The downgrade cannot restore the original offsets, so take a snapshot before upgrading. Do not kill a long-running migration: it commits batch by batch and the next upgrade resumes it. A converting user's first-conversion day in the daily results can move by one day; totals do not change.
* **safety:** rollback_percentage on the safety config and the rollback route's percentage parameter accept 0–100; other values now answer 422. A stored value outside that range is clamped when used.
* **api:** During a concurrent change by another administrator, PUT /api/v1/admin/users/{id}, PUT /api/v1/users/{id}, both DELETE routes and PATCH /api/v1/admin/users/{id} can answer 400 "Inactive user" or 403 "Not enough permissions" instead of 200/204. A database lock wait on these routes is bounded; a request that waits longer answers 500.
* **api:** an archived feature flag can no longer be turned on directly. `PUT /api/v1/feature-flags/{id}` with `is_active: true`, `POST /api/v1/feature-flags/{id}/activate`, `POST /api/v1/feature-flags/{id}/enable` and `POST /api/v1/feature-flags/{id}/toggle` answered 200 and made an archived flag ACTIVE. They now answer 400 with the detail "This flag is archived. Unarchive it before turning it on." and leave the flag archived. `POST /api/v1/feature-flags/bulk-toggle` with `enable` reports an archived flag with `success: false` and that sentence as its error, and processes the other flags. `POST /api/v1/feature-flags/{id}/deactivate`, `/disable` and bulk-toggle `disable` used to move an archived flag to INACTIVE; they now answer 200 and leave it ARCHIVED. The way out is the new beta route `POST /api/v1/feature-flags/{id}/unarchive`, which moves an archived flag to INACTIVE.
* **api:** PUT /api/v1/admin/users/{id} and PUT /api/v1/users/{id} refuse, with 400, a request that removes the caller's own superuser access or deactivates the caller's own account. Resending the stored values is unchanged.
* **api:** a feature flag created without `is_active` is now off; send `"is_active": true` to create it on. Flag create and update refuse unknown fields with 422, refuse an explicit null on `key`, `name`, `is_active`, `rollout_percentage` and `default_value`, and refuse a `status` that differs from the flag's. `default_value` accepts only `false`; `true` answers 422. Flag responses no longer carry `rules` (use `targeting_rules`), `variants`, `metrics` or `last_evaluated`, and `owner_id` is null rather than the string `"None"` when the flag has no owner. A safety rollback still lowers only the global rollout percentage: users matched by a targeting rule keep the flag ([#629](https://github.com/getexperimently/experimently/issues/629)).
* **auth:** under AUTH_PROVIDER=cognito, POST /api/v1/auth/signup and /api/v1/auth/confirm answer 404 unless COGNITO_SELF_SIGNUP_ENABLED=true.
* **results:** on GET /api/v1/results/{experiment_id}/sample-size, `required_sample_size_per_variant`, `achieved_power` and `baseline_rate` may now be null (200 with `unavailable_reason` when there is nothing to plan from). When `baseline_conversion_rate` is omitted the plan now starts from the control variant's observed rate instead of a fixed 0.1, and `current_sample_size_per_variant` is the smallest variant instead of the total divided by two. `SampleSizeResult` gains the required fields `alpha` and `comparisons`, and `guide_only_reasons` among the new optional ones.
* **auth:** under AUTH_PROVIDER=cognito, a sign-in is linked to its account by the Cognito user ID, stored as users.external_id = "cognito:<sub>". An account that has the Cognito username but is not linked is not used: this covers accounts created by earlier Cognito sign-ins, accounts with a password on the platform, and accounts linked to another identity. Until an administrator links it (docs/cognito_integration.md, "Linking an existing account"), the sign-in answers 401. A first sign-in from an identity with no email address, or with an address another account already has in any letter case, is refused. Roles come from the groups in the user's access token (the cognito:groups claim), so a group change takes effect at the user's next access token. ```
* **auth:** COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID are both required under AUTH_PROVIDER=cognito; with either unset every sign-in is refused. A token issued to a different app client is refused.
* **users:** an upgrade refuses while two accounts' email addresses differ only in letter case. Nothing is changed; resolve the accounts it names by id with docs/self-hosting/migrations.md#email-addresses-that-differ-only-in-case, then run the upgrade again.
* **experiments:** PUT /api/v1/experiments/{id} answers 400 instead of 403 when the experiment's state does not allow the change; 403 now means only that the caller's role may not update experiments. `schedule` on PUT /api/v1/experiments/{id} is refused with 422 whatever its value; schedule an experiment with PUT /api/v1/experiments/{id}/schedule.
* **users:** POST /api/v1/users/, PUT /api/v1/users/{id} and PUT /api/v1/admin/users/{id} answer 409 "Email already registered" when another account holds the address in any letter case (previously a case variant was accepted).
* **experiments:** DELETE /api/v1/experiments/{id} on a non-DRAFT experiment answers 400 (was 403), same detail; a caller without permission still gets 403.
* **users:** UserResponse.email may be null (GET/PUT /api/v1/users/{id}, GET /api/v1/users/, GET /api/v1/users/me, GET /api/v1/admin/users, GET/PUT /api/v1/admin/users/{id}).
* **experiments:** PUT /api/v1/experiments/{id}/schedule on a DRAFT experiment now keeps a start_date or end_date the request omits; send null to clear it (previously an omitted date was cleared). schedule.time_zone must be an IANA zone name of at most 64 characters, on /schedule and inside PUT /api/v1/experiments/{id}; anything else answers 422 (on PUT /experiments/{id} an unknown name was previously accepted and ignored).
* **experiments:** each experiment's partial rollout admits its own users ([#587](https://github.com/getexperimently/experimently/issues/587))
* **analysis:** `POST /api/v1/results/{experiment_id}/post-stratification` now answers 501 "not available yet" for every existing experiment (unknown experiment 404, malformed body 422, unchanged) and is marked beta.

### Features

* **api:** PATCH /api/v1/admin/users/{id} changes a user's role or active status (beta) ([#670](https://github.com/getexperimently/experimently/issues/670)) ([ba87486](https://github.com/getexperimently/experimently/commit/ba874869064c8f7cd1e5954cbcedad158ca0eae3))
* **api:** the feature-flag create and update contract: new flags start off, unknown fields answer 422, one flag response ([#706](https://github.com/getexperimently/experimently/issues/706)) ([b16cd83](https://github.com/getexperimently/experimently/commit/b16cd83770be4f60f852e971daee27ad76818348))
* **db:** feature flags store a default_value, false on every row ([#698](https://github.com/getexperimently/experimently/issues/698)) ([35facd1](https://github.com/getexperimently/experimently/commit/35facd133a15c7c64222328870e0358bb1ba82fb))
* **sdk:** the JS and Python SDKs are 0.1.0 beta and carry repository metadata ([#616](https://github.com/getexperimently/experimently/issues/616)) ([fca2ade](https://github.com/getexperimently/experimently/commit/fca2ade4c22bd00f4e56cc46c1db328e5459aed5))


### Bug Fixes

* **analysis:** post-stratification answers 501 instead of made-up numbers; event times compare in UTC ([#577](https://github.com/getexperimently/experimently/issues/577)) ([53208ff](https://github.com/getexperimently/experimently/commit/53208ff27d3f0422b6bf7d3994547a2a400f4491))
* **api:** a duplicate flag key answers 409, and the flag collection answers without a trailing slash ([#650](https://github.com/getexperimently/experimently/issues/650)) ([d4eade9](https://github.com/getexperimently/experimently/commit/d4eade9f63c7d03b0b410b339cf73290806704ea))
* **api:** a superuser cannot remove their own superuser access or deactivate themselves through PUT ([#712](https://github.com/getexperimently/experimently/issues/712)) ([f31bd84](https://github.com/getexperimently/experimently/commit/f31bd84d206c26e887741bc202bcc97d79854a5f))
* **api:** an archived flag refuses to be turned on until it is unarchived ([#716](https://github.com/getexperimently/experimently/issues/716)) ([3536b94](https://github.com/getexperimently/experimently/commit/3536b949982f50cc9ed53a96a7a6efa013e75017))
* **api:** experiment create retries a taken generated key ([#597](https://github.com/getexperimently/experimently/issues/597)) ([8651f66](https://github.com/getexperimently/experimently/commit/8651f66b6abea6274203db2cadc4575e032e404d))
* **api:** feature-flag targeting rules are validated when saved ([#743](https://github.com/getexperimently/experimently/issues/743)) ([69ab586](https://github.com/getexperimently/experimently/commit/69ab58636ea95b7058612511a0c930c229383b32))
* **api:** the admin user list filters by a search term ([#676](https://github.com/getexperimently/experimently/issues/676)) ([ded4f0f](https://github.com/getexperimently/experimently/commit/ded4f0f55207cfeb64848ef87feced9bdfd23c19))
* **api:** the flag list and detail are always read from the database ([#680](https://github.com/getexperimently/experimently/issues/680)) ([27dd7be](https://github.com/getexperimently/experimently/commit/27dd7bee91c32df57ba3ca082b150e9396046d71))
* **api:** the OpenFeature flag list no longer fails on a stored non-object targeting value ([#739](https://github.com/getexperimently/experimently/issues/739)) ([f4ef5eb](https://github.com/getexperimently/experimently/commit/f4ef5eb5dbb30747e145bbeea46ca8db1a6b9ba2))
* **api:** toggling an active flag turns it off ([#677](https://github.com/getexperimently/experimently/issues/677)) ([0632d4f](https://github.com/getexperimently/experimently/commit/0632d4f4c8b8be68ee2ac3bbb65689a91319ecce))
* **api:** two superusers acting on each other cannot leave none active ([#723](https://github.com/getexperimently/experimently/issues/723)) ([e21e336](https://github.com/getexperimently/experimently/commit/e21e336923d079f6da7866eb76d9729e0ba86615))
* **assignment:** remove the undeployed Lambda assignment copy, which did not enforce mutual exclusion or holdout ([#606](https://github.com/getexperimently/experimently/issues/606)) ([1599459](https://github.com/getexperimently/experimently/commit/1599459c95408ff8ed3c70c5b7360ef0ac3be1a8))
* **auth:** a Cognito sign-in is linked to its account by the Cognito user ID, not the username ([#683](https://github.com/getexperimently/experimently/issues/683)) ([f982dbd](https://github.com/getexperimently/experimently/commit/f982dbd093fc0bb7263171c3677bde19edfab8bc))
* **auth:** a Cognito sign-in that Cognito answers with a challenge gets 401 instead of 500 ([#701](https://github.com/getexperimently/experimently/issues/701)) ([f1b950a](https://github.com/getexperimently/experimently/commit/f1b950a37312fc606f4c56c070405f3ae39f4873))
* **auth:** Cognito sign-in accepts only access tokens issued to the configured user pool and app client ([#671](https://github.com/getexperimently/experimently/issues/671)) ([6dcc67d](https://github.com/getexperimently/experimently/commit/6dcc67d1b82848ffd973d4ff08a52330b9cde052))
* **auth:** sign-up and confirmation answer 404 under Cognito unless COGNITO_SELF_SIGNUP_ENABLED is set ([#705](https://github.com/getexperimently/experimently/issues/705)) ([a667823](https://github.com/getexperimently/experimently/commit/a6678237968c70d057abb3c0a38d124289077a7c))
* **bandit:** the PostgreSQL fallback counts converting users, as /results does ([#576](https://github.com/getexperimently/experimently/issues/576)) ([27cd938](https://github.com/getexperimently/experimently/commit/27cd938f8e9abc007301a6a67a765e831b55813e))
* **dashboard:** a failed sample-size request no longer blanks the results page, and the Overview card says what its check is ([#695](https://github.com/getexperimently/experimently/issues/695)) ([20fdc18](https://github.com/getexperimently/experimently/commit/20fdc18b6e8c130b847d719a42b9cab345100a3a))
* **dashboard:** an administrator can edit a user's role and active status ([#681](https://github.com/getexperimently/experimently/issues/681)) ([a8b62f0](https://github.com/getexperimently/experimently/commit/a8b62f00f0e99b584be11d8d6eac6ab82cd020a1))
* **dashboard:** saving a flag never erases targeting rules the editor cannot show ([#741](https://github.com/getexperimently/experimently/issues/741)) ([5664d87](https://github.com/getexperimently/experimently/commit/5664d87162b824224fe8cb9e2c923bde942b073c))
* **dashboard:** the admin user list pages and searches, and three pages send the parameters their API reads ([#679](https://github.com/getexperimently/experimently/issues/679)) ([bc51ac5](https://github.com/getexperimently/experimently/commit/bc51ac58d4d37c0ca0250a31b4259256c3e2794c))
* **dashboard:** the safety rollback dialog is keyboard-usable and the safety card shows when a flag is off ([#731](https://github.com/getexperimently/experimently/issues/731)) ([f4aa7fe](https://github.com/getexperimently/experimently/commit/f4aa7fe77019428f9fea035c30c2ceee5a3f9411))
* **deploy:** the secrets preflight checks each task definition's secret references, not names ([#643](https://github.com/getexperimently/experimently/issues/643)) ([a8d5ab9](https://github.com/getexperimently/experimently/commit/a8d5ab9ee3910afc032550d088f232f708141510))
* **deps:** THIRD_PARTY_LICENSES.md records brace-expansion 1.1.21 ([#586](https://github.com/getexperimently/experimently/issues/586)) ([d194404](https://github.com/getexperimently/experimently/commit/d194404a63f71f011e865978d6606ac421f524a4))
* **docker:** the dashboard image takes Alpine's package updates at build time ([#582](https://github.com/getexperimently/experimently/issues/582)) ([c391282](https://github.com/getexperimently/experimently/commit/c391282d325e6462ad779a746bf7954eb792c172))
* **etl:** the status routes read only the configured job and crawler names ([#661](https://github.com/getexperimently/experimently/issues/661)) ([a23880e](https://github.com/getexperimently/experimently/commit/a23880e07a30a738280ddd31b431fbb977820b62))
* **events:** stored event times are rewritten to UTC, so old and new events sort together ([#728](https://github.com/getexperimently/experimently/issues/728)) ([dc08602](https://github.com/getexperimently/experimently/commit/dc086023298d444f7fce25d4cc2441326d2dce42))
* **experiments:** a clone of an experiment with a long name is shortened to fit ([#672](https://github.com/getexperimently/experimently/issues/672)) ([2a6c728](https://github.com/getexperimently/experimently/commit/2a6c72871a6995838e4dfe37182bae90a6c14360))
* **experiments:** a cloned experiment gets its own key ([#618](https://github.com/getexperimently/experimently/issues/618)) ([cc3d98c](https://github.com/getexperimently/experimently/commit/cc3d98cd396a99d7d1538c4c69ab3059d7b50719))
* **experiments:** a cloned experiment keeps its analysis settings ([#608](https://github.com/getexperimently/experimently/issues/608)) ([47e8ae2](https://github.com/getexperimently/experimently/commit/47e8ae2e9bbbff32a73720d5a469c69999c69ee1))
* **experiments:** a schedule update keeps the fields it omits and refuses an unknown time zone ([#594](https://github.com/getexperimently/experimently/issues/594)) ([b0786ec](https://github.com/getexperimently/experimently/commit/b0786ecb4bb469671a84036662187d1a811c57af))
* **experiments:** deleting a non-draft experiment answers 400, not 403 ([#604](https://github.com/getexperimently/experimently/issues/604)) ([f7c3d06](https://github.com/getexperimently/experimently/commit/f7c3d06cf70dea9d5f524b33e61081014f6ae2bf))
* **experiments:** each experiment's partial rollout admits its own users ([#587](https://github.com/getexperimently/experimently/issues/587)) ([dfa6689](https://github.com/getexperimently/experimently/commit/dfa6689bae73c479b9bd8a917895395978018fce))
* **experiments:** PUT /experiments/{id} refuses a schedule and answers 400 when the state refuses the change ([#654](https://github.com/getexperimently/experimently/issues/654)) ([e714340](https://github.com/getexperimently/experimently/commit/e7143408e172a4db4498c94d29eebcf93052932f))
* **experiments:** sample-size routes refuse a size that is not finite with 422 ([#732](https://github.com/getexperimently/experimently/issues/732)) ([f8b27ad](https://github.com/getexperimently/experimently/commit/f8b27ad855f9006692b43df638b1d512a5326a38))
* **experiments:** the sample-size estimate is right at every significance and power the wizard offers ([#696](https://github.com/getexperimently/experimently/issues/696)) ([ea66160](https://github.com/getexperimently/experimently/commit/ea66160f1057839a3d28f80e4fd98101f5651b8b))
* **experiments:** the wizard estimate uses the same formula as the results page ([#702](https://github.com/getexperimently/experimently/issues/702)) ([dfd4b10](https://github.com/getexperimently/experimently/commit/dfd4b10cc6e5f0178fe9ab6f182c4300543e1302))
* **flags:** a legacy targeting condition with an operator outside eq/ne/gt/lt/contains/in no longer matches ([#740](https://github.com/getexperimently/experimently/issues/740)) ([eac6366](https://github.com/getexperimently/experimently/commit/eac6366006d11f7fd032628660671ade149a12d2))
* **infra:** the API task may use the Glue job, crawler and catalog it is told about ([#678](https://github.com/getexperimently/experimently/issues/678)) ([5232d69](https://github.com/getexperimently/experimently/commit/5232d699da7fce7d4cd3cd527fe10364d3008142))
* **infra:** the reference Cognito user pool accepts administrator-created users only ([#626](https://github.com/getexperimently/experimently/issues/626)) ([7ff66d4](https://github.com/getexperimently/experimently/commit/7ff66d4e1cc0b6d8266e354c0fac5731de48b998))
* **results:** a Bayesian winner needs P(best) of at least 0.975 ([#596](https://github.com/getexperimently/experimently/issues/596)) ([38c1437](https://github.com/getexperimently/experimently/commit/38c143766e0a49c847b995ef0ccf909bb03dd43b))
* **results:** intervals and significance use the requested confidence level ([#584](https://github.com/getexperimently/experimently/issues/584)) ([5e3b5cf](https://github.com/getexperimently/experimently/commit/5e3b5cf67edac5c1e889008ea86b4aacf60be192))
* **results:** results show one significance decision and no misleading units ([#583](https://github.com/getexperimently/experimently/issues/583)) ([33b021b](https://github.com/getexperimently/experimently/commit/33b021b5d17a369a750bda6078bca8232f2cfc22))
* **results:** the Sample Size tab plans from the experiment's own control rate ([#703](https://github.com/getexperimently/experimently/issues/703)) ([9eb8957](https://github.com/getexperimently/experimently/commit/9eb89570814d7665f246998d4d7ef23b13b480c2))
* **results:** the sequential confidence sequence narrows and agrees with the stop decision ([#590](https://github.com/getexperimently/experimently/issues/590)) ([d687de3](https://github.com/getexperimently/experimently/commit/d687de3bf1984d6d10ba785e056e56ccebe64f00))
* **rollouts:** a paused rollout schedule is never advanced by a tick or an advance already in flight ([#722](https://github.com/getexperimently/experimently/issues/722)) ([2cc9667](https://github.com/getexperimently/experimently/commit/2cc9667384cd63d3862bbe67b05f488619f8ec0d))
* **rollouts:** one failing schedule no longer stops the rest of the tick ([#599](https://github.com/getexperimently/experimently/issues/599)) ([ea2aee5](https://github.com/getexperimently/experimently/commit/ea2aee5856840b2f40fa1ce6728dffe1e4206770))
* **safety:** a manual rollback records who ran it ([#721](https://github.com/getexperimently/experimently/issues/721)) ([12b582b](https://github.com/getexperimently/experimently/commit/12b582bf173d2c35618f3cd3575f38fcdf9876bc))
* **safety:** a safety rollback to 0% turns the flag off for every user and pauses its rollout schedule ([#729](https://github.com/getexperimently/experimently/issues/729)) ([9551029](https://github.com/getexperimently/experimently/commit/955102909df8bbe1e4a762e6465c3f134834fbc9))
* **safety:** rollback percentages outside 0–100 are refused ([#725](https://github.com/getexperimently/experimently/issues/725)) ([289d8b6](https://github.com/getexperimently/experimently/commit/289d8b64032e188d8e60c41d4da434ca49a809a6))
* **scheduler:** one failing experiment no longer undoes the rest of the tick ([#592](https://github.com/getexperimently/experimently/issues/592)) ([f9f1e79](https://github.com/getexperimently/experimently/commit/f9f1e79131ac7c8ac85a9e99bd946614960d8bde))
* **sdk:** the OpenFeature Python provider accepts the 0.1 Python SDK ([#686](https://github.com/getexperimently/experimently/issues/686)) ([60f38e7](https://github.com/getexperimently/experimently/commit/60f38e763dd3d0591f57c767f7d8e21cd8ac16ce))
* **sso:** sign-in fits long names and refuses an email address too long to store ([#713](https://github.com/getexperimently/experimently/issues/713)) ([72c7916](https://github.com/getexperimently/experimently/commit/72c79160968524c48d4ed688fed9d40912f3f5d6))
* **tests:** role-client scan sees direct overrides; drop dead init_db; fix SSO is_enforced text ([#628](https://github.com/getexperimently/experimently/issues/628)) ([25c9a85](https://github.com/getexperimently/experimently/commit/25c9a85695951359a4b46c9401f328277752f62d))
* **users:** a duplicate username answers 409, and the dashboard's experiment delete sends experiment_key ([#623](https://github.com/getexperimently/experimently/issues/623)) ([a9293f0](https://github.com/getexperimently/experimently/commit/a9293f001ba0d8730edbf795e8b784c053efbb65))
* **users:** an account with no email address no longer makes the user endpoints answer 500 ([#600](https://github.com/getexperimently/experimently/issues/600)) ([179d2bf](https://github.com/getexperimently/experimently/commit/179d2bffcbdc71e5697698bf334a87a77f88983b))
* **users:** creating or updating an account with another account's email address in different letter case answers 409 ([#612](https://github.com/getexperimently/experimently/issues/612)) ([9be6751](https://github.com/getexperimently/experimently/commit/9be6751bcf5afae1a777165ffb28b20d40f2a685))
* **users:** email addresses are unique regardless of letter case, and an upgrade refuses while two accounts differ only in case ([#653](https://github.com/getexperimently/experimently/issues/653)) ([54437d2](https://github.com/getexperimently/experimently/commit/54437d26b31541581e0dc8d0d83684645fe42d58))
* **users:** GET /users/me returns the caller's role ([#710](https://github.com/getexperimently/experimently/issues/710)) ([a0c2af9](https://github.com/getexperimently/experimently/commit/a0c2af93025853b439b74f138de610dd29656485))


### Documentation

* **api:** two flag route descriptions no longer mention a cache ([#690](https://github.com/getexperimently/experimently/issues/690)) ([d8eeb09](https://github.com/getexperimently/experimently/commit/d8eeb09d725194a3ad194d8c4f2933e3ecfde369))
* **auth:** the dashboard does not sign in under AUTH_PROVIDER=cognito; use the API ([#711](https://github.com/getexperimently/experimently/issues/711)) ([11449da](https://github.com/getexperimently/experimently/commit/11449da666b7c0b99fcf559fe72cd5a0f2dca5b8))
* **safety:** a rollback lowers the global rollout; users matched by a targeting rule keep the flag ([#634](https://github.com/getexperimently/experimently/issues/634)) ([67e50df](https://github.com/getexperimently/experimently/commit/67e50df0e00ed8df057058b7f7ff72b72db5b74c))
* **site:** correct SDK, AWS and capability claims on the landing page ([#645](https://github.com/getexperimently/experimently/issues/645)) ([4002103](https://github.com/getexperimently/experimently/commit/400210374a10b63091c337b0296d428d7aa33a00))
* state what the dashboard, the native SDKs and sign-in do today ([#625](https://github.com/getexperimently/experimently/issues/625)) ([f3ec955](https://github.com/getexperimently/experimently/commit/f3ec9553f251089e03bd058052dab07578b6e93b))


### Build System

* make venv installs the modules' requirements in a full checkout ([#585](https://github.com/getexperimently/experimently/issues/585)) ([38bca26](https://github.com/getexperimently/experimently/commit/38bca266b28fa327c82e9dc30329af7162edca64))

## [0.16.2](https://github.com/getexperimently/experimently/compare/v0.16.1...v0.16.2) (2026-10-01)


### Bug Fixes

* **api:** audit-log filters name the accepted values in their 400 ([#568](https://github.com/getexperimently/experimently/issues/568)) ([af712b1](https://github.com/getexperimently/experimently/commit/af712b1fe95c5f9e8939913f9ec2e8819f05b969))
* **api:** unexpected errors answer a fixed message with the request ID ([#567](https://github.com/getexperimently/experimently/issues/567)) ([d19741a](https://github.com/getexperimently/experimently/commit/d19741adda67ba6fdbd2e9699a4ed1ff09a6e390))

## [0.16.1](https://github.com/getexperimently/experimently/compare/v0.16.0...v0.16.1) (2026-10-01)


### Bug Fixes

* **api:** ETL job and crawler failures answer a fixed message with the request ID ([#563](https://github.com/getexperimently/experimently/issues/563)) ([cb791e2](https://github.com/getexperimently/experimently/commit/cb791e2f8d46f419379df33394272fd0db7e828b))
* **api:** wizard steps and experiment numbers out of range answer 422 ([#562](https://github.com/getexperimently/experimently/issues/562)) ([00e6dc7](https://github.com/getexperimently/experimently/commit/00e6dc716f4f00893f9e9a7d8282e3b650799d63))

## [0.16.0](https://github.com/getexperimently/experimently/compare/v0.15.0...v0.16.0) (2026-10-01)


### ⚠ BREAKING CHANGES

* **api:** an experiment's status changes only through start, pause, complete and archive ([#553](https://github.com/getexperimently/experimently/issues/553))
* **api:** experiment targeting rules are validated on create ([#539](https://github.com/getexperimently/experimently/issues/539))
* **api:** an experiment's targeting can be changed only while it is draft or paused ([#538](https://github.com/getexperimently/experimently/issues/538))

### Features

* **dashboard:** see and change who can join an experiment ([#556](https://github.com/getexperimently/experimently/issues/556)) ([3572180](https://github.com/getexperimently/experimently/commit/3572180f36d74cfaae38819bbbe37f9aa2e10935))


### Bug Fixes

* **api:** an experiment update refuses null for required fields ([#550](https://github.com/getexperimently/experimently/issues/550)) ([be77284](https://github.com/getexperimently/experimently/commit/be772847ca09f3c23d6d86c9d86f629a5cce2446))
* **api:** an experiment's status changes only through start, pause, complete and archive ([#553](https://github.com/getexperimently/experimently/issues/553)) ([d5fdea5](https://github.com/getexperimently/experimently/commit/d5fdea57c63967cc4f4b12183168bafcd09a40ae))
* **api:** an experiment's targeting can be changed only while it is draft or paused ([#538](https://github.com/getexperimently/experimently/issues/538)) ([63c4184](https://github.com/getexperimently/experimently/commit/63c4184b2cb4467576c5cac8daf4b6fa7bf28cae))
* **api:** experiment and results errors answer a fixed message with the request ID ([#545](https://github.com/getexperimently/experimently/issues/545)) ([15ab23b](https://github.com/getexperimently/experimently/commit/15ab23b10457fdc0b1c1590351c415c026c46779))
* **api:** experiment metric fields over their length answer 422 ([#555](https://github.com/getexperimently/experimently/issues/555)) ([0029f17](https://github.com/getexperimently/experimently/commit/0029f17f59c8c0f0bf70938111335621032a71ef))
* **api:** experiment metrics keep their type and can be replaced by name ([#561](https://github.com/getexperimently/experimently/issues/561)) ([3a33ddd](https://github.com/getexperimently/experimently/commit/3a33dddc1d6adb9e8e80fc14c00dab9b2052fbcb))
* **api:** experiment targeting rules are validated on create ([#539](https://github.com/getexperimently/experimently/issues/539)) ([33bd440](https://github.com/getexperimently/experimently/commit/33bd440bdf4ef6156622c9f8855baf6df6409a0d))
* **api:** SDK routes answer 422 for a NUL or unpaired surrogate in the path ([#548](https://github.com/getexperimently/experimently/issues/548)) ([8ab4df6](https://github.com/getexperimently/experimently/commit/8ab4df6266fb54a55398718d753d0bd0379a56b8))
* **api:** tracking requests with NUL or unpaired surrogate characters answer 422 ([#546](https://github.com/getexperimently/experimently/issues/546)) ([79e728a](https://github.com/getexperimently/experimently/commit/79e728acbc8280bb0e13c29f046425a555175f8a))

## [0.15.0](https://github.com/getexperimently/experimently/compare/v0.14.0...v0.15.0) (2026-09-30)


### ⚠ BREAKING CHANGES

* **api:** experiment targeting rules are validated on update ([#532](https://github.com/getexperimently/experimently/issues/532))

### Features

* **analysis:** mean-metric results from sufficient statistics ([#510](https://github.com/getexperimently/experimently/issues/510)) ([748839d](https://github.com/getexperimently/experimently/commit/748839d67759ab13509ac7531cbd50c3c5c61ae0))
* **warehouse:** mean metrics are analysed in warehouse runs ([#522](https://github.com/getexperimently/experimently/issues/522)) ([332b8db](https://github.com/getexperimently/experimently/commit/332b8db514fb3c47aaa4976ac9fb2abbb9be1b6d))


### Bug Fixes

* **api:** bad sample-size input answers 422, not 500 ([#528](https://github.com/getexperimently/experimently/issues/528)) ([59d5beb](https://github.com/getexperimently/experimently/commit/59d5beb911f9eaf0b03610d9ace2311ba87e945f))
* **api:** experiment targeting rules are validated on update ([#532](https://github.com/getexperimently/experimently/issues/532)) ([679eb40](https://github.com/getexperimently/experimently/commit/679eb40359658294479d900d6a1db92a99e77ee5))
* **api:** wizard drafts are per user; the wizard endpoints are deprecated ([#530](https://github.com/getexperimently/experimently/issues/530)) ([1307b7f](https://github.com/getexperimently/experimently/commit/1307b7f071e9a411f3904f045fbb27fab8e4d3c7))
* **deps:** PyJWT 2.15.1 ([#534](https://github.com/getexperimently/experimently/issues/534)) ([fef205b](https://github.com/getexperimently/experimently/commit/fef205b6df9ae0772a9f76325b0b57e23f3ac6e2))
* **deps:** urllib3 2.8.0 in both image locks ([#527](https://github.com/getexperimently/experimently/issues/527)) ([af54af2](https://github.com/getexperimently/experimently/commit/af54af2dc2d9be1ae7afa59203ba2755dcc84e94))

## [0.14.0](https://github.com/getexperimently/experimently/compare/v0.13.0...v0.14.0) (2026-09-30)


### ⚠ BREAKING CHANGES

* **users:** a client that changes the signed-in user's own password must use `POST /api/v1/users/me/password` with `current_password` and `new_password`. Sending your own `password` to `PUT /api/v1/users/{user_id}` or `PUT /api/v1/admin/users/{user_id}` now returns 403. An empty or weak `password` on those routes returns 422. `PUT /api/v1/admin/users/{user_id}` now resets another user's password.
* **infra:** API tasks no longer run migrations; the deploy's migration task does ([#499](https://github.com/getexperimently/experimently/issues/499))

### Features

* **infra:** API tasks no longer run migrations; the deploy's migration task does ([#499](https://github.com/getexperimently/experimently/issues/499)) ([a6401ec](https://github.com/getexperimently/experimently/commit/a6401ec81aa8e430493ff58a80ea34b94d738a53))


### Bug Fixes

* **api:** validation errors no longer repeat the submitted values ([#509](https://github.com/getexperimently/experimently/issues/509)) ([756e1f4](https://github.com/getexperimently/experimently/commit/756e1f4d114a5b27eacf9babab3c75432538c220))
* **bootstrap:** a first-admin password longer than 72 bytes is refused with a clear message ([#506](https://github.com/getexperimently/experimently/issues/506)) ([69eb5ca](https://github.com/getexperimently/experimently/commit/69eb5cafa7d0fd43df0406216c1490a790c564de))
* **demo:** the setup and teardown scripts and the rollout story work as documented ([#491](https://github.com/getexperimently/experimently/issues/491)) ([6088458](https://github.com/getexperimently/experimently/commit/6088458ab9bc1e239b9049b649c1ba557fc19eec))
* **docker:** the backend image installs the current OpenSSL packages ([#514](https://github.com/getexperimently/experimently/issues/514)) ([27fbaa7](https://github.com/getexperimently/experimently/commit/27fbaa70ccff390c69b1284d2e3f726421ab910c))
* **docs:** the doc-examples runner measures teardown and survives a dropped connection ([#494](https://github.com/getexperimently/experimently/issues/494)) ([baac924](https://github.com/getexperimently/experimently/commit/baac924f93f0a76df17b5a35361a4bcb846ca9f5))
* **infra:** the API task is given the counters table name and access to it ([#488](https://github.com/getexperimently/experimently/issues/488)) ([3b84714](https://github.com/getexperimently/experimently/commit/3b8471464809c9d12809d8ad34d8658ab46e893d))
* **users:** changing your own password requires the current password ([#516](https://github.com/getexperimently/experimently/issues/516)) ([34dae20](https://github.com/getexperimently/experimently/commit/34dae2085e76cabadb6ed7cdc0d20ec7e75ae283))


### Documentation

* **api:** /edge/bootstrap is deprecated in favour of the ruleset route ([#495](https://github.com/getexperimently/experimently/issues/495)) ([1cbadfd](https://github.com/getexperimently/experimently/commit/1cbadfdd48d0a6fca2cf44d5d834f0ec43978c7e))
* links to the API docs point at /api/v1/docs ([#493](https://github.com/getexperimently/experimently/issues/493)) ([3633da6](https://github.com/getexperimently/experimently/commit/3633da6e8fe3bf778e8dc6d70df6a7e8a08c5202))

## [0.13.0](https://github.com/getexperimently/experimently/compare/v0.12.0...v0.13.0) (2026-09-30)


### Features

* **api:** a setting for how often the experiment scheduler runs ([#486](https://github.com/getexperimently/experimently/issues/486)) ([d127407](https://github.com/getexperimently/experimently/commit/d127407b84f58539e32a1c4f6363a6466775302a))
* **db:** experiments gain a nullable resume_at column ([#477](https://github.com/getexperimently/experimently/issues/477)) ([2ea6580](https://github.com/getexperimently/experimently/commit/2ea65803d35c48ece136902a8ce5f80f847693cc))


### Bug Fixes

* **api:** a warehouse run's SQL is returned to analysts and above ([#468](https://github.com/getexperimently/experimently/issues/468)) ([94654a6](https://github.com/getexperimently/experimently/commit/94654a6accf19de3b4f79fc77f08f82fe5e2824a))
* **api:** admins and developers can schedule and delete any experiment ([#469](https://github.com/getexperimently/experimently/issues/469)) ([9c9715c](https://github.com/getexperimently/experimently/commit/9c9715ca30809f5df0442cb3ba2b0495f05a7411))
* **api:** audit records for experiment and flag changes are saved ([#475](https://github.com/getexperimently/experimently/issues/475)) ([b275b26](https://github.com/getexperimently/experimently/commit/b275b26bb78ba0ad3f3294dc41373e9893cb480d))
* **assignment:** mutual exclusion holds when a sibling experiment is activated ([#478](https://github.com/getexperimently/experimently/issues/478)) ([edb5071](https://github.com/getexperimently/experimently/commit/edb5071639b8dc973ae5f44ae72fae47825c7d41))
* **dashboard:** the View SQL button appears only for roles that can see it ([#472](https://github.com/getexperimently/experimently/issues/472)) ([04b8b4a](https://github.com/getexperimently/experimently/commit/04b8b4a4ccf5d4b19e6eb41343894a130d7d22c0))
* **experiments:** a paused experiment stays paused until it is started or a resume is scheduled ([#485](https://github.com/getexperimently/experimently/issues/485)) ([b363453](https://github.com/getexperimently/experimently/commit/b363453a6c2130cee562dded326caa8135fb11bd))


### Documentation

* CLAUDE.md states the bandit source rule and drops a note about a removed function ([#463](https://github.com/getexperimently/experimently/issues/463)) ([817cd8a](https://github.com/getexperimently/experimently/commit/817cd8ab2473781ce822a423640d939d32e7daaa))

## [0.12.0](https://github.com/getexperimently/experimently/compare/v0.11.0...v0.12.0) (2026-09-29)


### Features

* **dashboard:** run a warehouse analysis and read its results (beta) ([#421](https://github.com/getexperimently/experimently/issues/421)) ([2b5931a](https://github.com/getexperimently/experimently/commit/2b5931a1bef76ce6dc088a7ee9f9813f5ccfe599))
* **dashboard:** warehouse connections and metric sources (beta) ([#420](https://github.com/getexperimently/experimently/issues/420)) ([ca44cef](https://github.com/getexperimently/experimently/commit/ca44cef8c626e9552ef883765cc7b0bfc311df6e))
* **deploy:** refuse a deploy while an API 5xx alarm is firing ([#366](https://github.com/getexperimently/experimently/issues/366)) ([1366f1d](https://github.com/getexperimently/experimently/commit/1366f1dab9230012ead9d692315c42fbb86ff592))
* **sdk:** the JavaScript SDK evaluates flags locally from the ruleset (beta) ([#368](https://github.com/getexperimently/experimently/issues/368)) ([4263f60](https://github.com/getexperimently/experimently/commit/4263f60a90e54a3b193a1e64d73ec0bcf96eb8f6))
* **sdk:** the Python SDK evaluates flags locally from the ruleset (beta) ([#374](https://github.com/getexperimently/experimently/issues/374)) ([817963a](https://github.com/getexperimently/experimently/commit/817963a626f76fca5cf783ef77240563c52a7173))
* **warehouse:** BigQuery connector, disabled until verified ([#371](https://github.com/getexperimently/experimently/issues/371)) ([4137f8a](https://github.com/getexperimently/experimently/commit/4137f8a001126966ee0a193b3a3a2253a9e3af8d))
* **warehouse:** Snowflake connector with key-pair sign-in, disabled until verified ([#380](https://github.com/getexperimently/experimently/issues/380)) ([d9f2f91](https://github.com/getexperimently/experimently/commit/d9f2f91da9398a6cb1926d90e9604f43038864f7))
* **warehouse:** warehouse analyses for proportion metrics with SRM (beta) ([#412](https://github.com/getexperimently/experimently/issues/412)) ([01a96fc](https://github.com/getexperimently/experimently/commit/01a96fc297541ff022b514f87690864abbb902ac))


### Bug Fixes

* **api:** a failed lookup in a batch no longer fails the items after it ([#414](https://github.com/getexperimently/experimently/issues/414)) ([c3bbaca](https://github.com/getexperimently/experimently/commit/c3bbacab036e29c147e196a3719c0230ad1a505f))
* **api:** analysts and viewers can read every experiment's results, and change none ([#460](https://github.com/getexperimently/experimently/issues/460)) ([86aa27a](https://github.com/getexperimently/experimently/commit/86aa27a57a6b723de4bad78a2c36db0c5e6f0ea0))
* **api:** creating an experiment whose key is taken answers 409 with a plain message ([#395](https://github.com/getexperimently/experimently/issues/395)) ([90d48f4](https://github.com/getexperimently/experimently/commit/90d48f4177c7d2e42f0c42edd32dcdfbdbb8845d))
* **api:** experiment owners and permitted roles can open an experiment again ([#451](https://github.com/getexperimently/experimently/issues/451)) ([7342f48](https://github.com/getexperimently/experimently/commit/7342f48aec23197a8495297c91f0943d6ccc8410))
* **api:** experiment routes work with the cache enabled ([#437](https://github.com/getexperimently/experimently/issues/437)) ([ae5f091](https://github.com/getexperimently/experimently/commit/ae5f091e60e7d1fb3a83599aaf40f7d90005292d))
* **api:** feature-flag routes work with the flag cache enabled ([#427](https://github.com/getexperimently/experimently/issues/427)) ([493e782](https://github.com/getexperimently/experimently/commit/493e7824448306f3c3a9a7b4453ddaf07d382ed2))
* **api:** integer bounds in the API description fit a JSON number ([#462](https://github.com/getexperimently/experimently/issues/462)) ([d5f852b](https://github.com/getexperimently/experimently/commit/d5f852b241e73edfc6ef8bf1f5302c0ad2276796))
* **api:** tracking and error-report failures answer a short message with the request ID ([#411](https://github.com/getexperimently/experimently/issues/411)) ([9e96ba5](https://github.com/getexperimently/experimently/commit/9e96ba58b89c860a57d5af06e0a2c452ef25d7c2))
* **auth:** Cognito-only auth routes answer 404 when AUTH_PROVIDER is not cognito ([#397](https://github.com/getexperimently/experimently/issues/397)) ([e8a16f8](https://github.com/getexperimently/experimently/commit/e8a16f82c4c4177a598e6d2f81efcbdb50803c07))
* **bandit:** a partial DynamoDB count no longer replaces the complete PostgreSQL count ([#430](https://github.com/getexperimently/experimently/issues/430)) ([fb3f629](https://github.com/getexperimently/experimently/commit/fb3f629c3e9f1ea8348da57350ee77bf45c62758))
* **ci:** regression-guard no longer fails without a merge base or blocks on a cancelled run ([#418](https://github.com/getexperimently/experimently/issues/418)) ([d2c0afc](https://github.com/getexperimently/experimently/commit/d2c0afc9c3a73664450c029f67ebc2b3204d63d1))
* **dashboard:** creating an experiment keeps your answers on a sign-in or key error ([#415](https://github.com/getexperimently/experimently/issues/415)) ([6fd886c](https://github.com/getexperimently/experimently/commit/6fd886c3e3e9a170fa21e1669720a2e827b2e93f))
* **dashboard:** show the request ID once when the error message already includes it ([#403](https://github.com/getexperimently/experimently/issues/403)) ([da58d9a](https://github.com/getexperimently/experimently/commit/da58d9a906326f393db14f77d3b02b11f2aaf82a))
* **dashboard:** the experiment page offers changes only to roles that can make them ([#461](https://github.com/getexperimently/experimently/issues/461)) ([3e6bf38](https://github.com/getexperimently/experimently/commit/3e6bf3800ad3753469c7d4b73bdd46d965d9dbf8))
* **deploy:** rollback fails loudly when a stopped deployment does not finish ([bc7bb07](https://github.com/getexperimently/experimently/commit/bc7bb0764fe908d6bff5618dc1f76624b84a6089))
* **logging:** evaluation and assignment no longer log the user id ([#365](https://github.com/getexperimently/experimently/issues/365)) ([a254b59](https://github.com/getexperimently/experimently/commit/a254b59e53f86aa607150541721edb3a90069112))
* **rules:** contains_all and contains_any do not match a non-list attribute instead of raising ([#423](https://github.com/getexperimently/experimently/issues/423)) ([a66db26](https://github.com/getexperimently/experimently/commit/a66db263dc682de10cbe4a0a7f4aed207a6b06cf))
* **sdk:** the OpenFeature provider takes @openfeature/server-sdk as a peer dependency ([#367](https://github.com/getexperimently/experimently/issues/367)) ([2b1d6b2](https://github.com/getexperimently/experimently/commit/2b1d6b2c97d30edf0736618331abb832bf8c6004))
* **tests:** the rollout schedule API tests use the configured database ([#360](https://github.com/getexperimently/experimently/issues/360)) ([e45d7c1](https://github.com/getexperimently/experimently/commit/e45d7c156b98c4cefadfd2bb249b63b0cb496438))
* **workspaces:** show the invitation link after creating it, and fill in the invitation page ([#381](https://github.com/getexperimently/experimently/issues/381)) ([33a1a71](https://github.com/getexperimently/experimently/commit/33a1a71723da86ab792fa1a42c2f261826723688))
* **workspaces:** the invitation preview shows the invited address in full only to that account ([#372](https://github.com/getexperimently/experimently/issues/372)) ([59536d9](https://github.com/getexperimently/experimently/commit/59536d949eec42867b7d06aa95a368d320efc591))


### Dependencies

* **backend:** pytest 9 and pytest-asyncio 1.3 ([#359](https://github.com/getexperimently/experimently/issues/359)) ([f7f8de8](https://github.com/getexperimently/experimently/commit/f7f8de8cb5b866e7c096d0f34b5b9929a8c40f76))


### Documentation

* **deploy:** disaster-recovery.md describes what the CDK actually creates ([bc7bb07](https://github.com/getexperimently/experimently/commit/bc7bb0764fe908d6bff5618dc1f76624b84a6089))
* **llm:** the quickstart's completion steps are secret skips, not bug [#196](https://github.com/getexperimently/experimently/issues/196) ([bc7bb07](https://github.com/getexperimently/experimently/commit/bc7bb0764fe908d6bff5618dc1f76624b84a6089))
* pages describe the infrastructure the stacks actually create ([bc7bb07](https://github.com/getexperimently/experimently/commit/bc7bb0764fe908d6bff5618dc1f76624b84a6089))
* the FAQ describes dashboard deploys, and shell examples paste cleanly into zsh ([#422](https://github.com/getexperimently/experimently/issues/422)) ([664eab1](https://github.com/getexperimently/experimently/commit/664eab1cbe9cdccb61517020effc80fe5da31905))
* the secrets-management rotation steps render as a code block again ([#459](https://github.com/getexperimently/experimently/issues/459)) ([ddb2d31](https://github.com/getexperimently/experimently/commit/ddb2d311dff11981e5585e59dd407c6aa5e4f749))

## [0.11.0](https://github.com/getexperimently/experimently/compare/v0.10.0...v0.11.0) (2026-09-28)


### ⚠ BREAKING CHANGES

* **warehouse:** warehouse analysis tables; saved warehouse connections are removed ([#322](https://github.com/getexperimently/experimently/issues/322))
* **api:** a staging or production API with neither `CORS_ORIGINS` nor `BACKEND_CORS_ORIGINS` set no longer allows the localhost origins. A browser client on another origin (a local dashboard or demo app, or a site running the browser SDK) needs its origin in `CORS_ORIGINS` (Helm: `corsOrigins`), or in `DASHBOARD_ORIGINS` for a dashboard on its own host. Cross-origin responses no longer carry `Access-Control-Allow-Credentials`. Development and test are unchanged.
* **api:** POST /api/v1/openfeature/evaluate and POST /api/v1/openfeature/bulk-evaluate are removed and now answer 404. They could not evaluate an existing flag (they read a FeatureFlag.enabled attribute the model does not have), and neither provider called them. The OpenFeature providers use GET /api/v1/feature-flags/evaluate/{key} and are unaffected. GET /api/v1/openfeature/flags is deprecated and unchanged.

### Features

* **modules:** a bounded outbound layer for warehouse connectors ([#321](https://github.com/getexperimently/experimently/issues/321)) ([24b9958](https://github.com/getexperimently/experimently/commit/24b995842073fa52abdbc0cbb56bdd757125abdd))
* **tracking:** record locally evaluated flags so safety monitoring keeps its denominator (beta) ([#226](https://github.com/getexperimently/experimently/issues/226)) ([#296](https://github.com/getexperimently/experimently/issues/296)) ([8a64d22](https://github.com/getexperimently/experimently/commit/8a64d22725c91e5ee144e52cb8d47f8d06cf54ae))
* **warehouse:** warehouse analysis tables; saved warehouse connections are removed ([#322](https://github.com/getexperimently/experimently/issues/322)) ([5c5720f](https://github.com/getexperimently/experimently/commit/5c5720fd3776bc2867cf910f5ffdc850c2509401))


### Bug Fixes

* **analysis:** count converting users without joining events to assignments ([#356](https://github.com/getexperimently/experimently/issues/356)) ([bcd4a01](https://github.com/getexperimently/experimently/commit/bcd4a011bf4328b2ba3c7f14add9b2812556982b))
* **analysis:** every endpoint counts converting users, not conversion events ([#337](https://github.com/getexperimently/experimently/issues/337)) ([ca3e7b9](https://github.com/getexperimently/experimently/commit/ca3e7b9dc8395e5ee3940ba2ba05bc49aa2c97bb))
* **api:** remove the OpenFeature evaluate routes, which never returned a result, and deprecate /openfeature/flags ([#345](https://github.com/getexperimently/experimently/issues/345)) ([2373367](https://github.com/getexperimently/experimently/commit/2373367b8b8bbf18be9bf6b57cefafa2b4fb1be3))
* **api:** staging and production allow only configured browser origins ([#349](https://github.com/getexperimently/experimently/issues/349)) ([07a408c](https://github.com/getexperimently/experimently/commit/07a408c9bcd481933867e28425e6f8893a412004))
* **deploy:** workflow runs never print the AWS account ID ([#311](https://github.com/getexperimently/experimently/issues/311)) ([1812f6e](https://github.com/getexperimently/experimently/commit/1812f6e966a739f7b957ce88be63cc161dba1a1f))
* **sso:** SAML sign-in errors carry a fixed message, and the docs say SAML sign-in is not available yet ([#346](https://github.com/getexperimently/experimently/issues/346)) ([91f7e0f](https://github.com/getexperimently/experimently/commit/91f7e0f515527db511325125afc6adc582ce333b))
* **workspaces:** an invite can be accepted only by the account it was sent to ([#348](https://github.com/getexperimently/experimently/issues/348)) ([d5ec4ae](https://github.com/getexperimently/experimently/commit/d5ec4ae4efb8ff696203214f1e11e74b6c93de8b))

## [0.10.0](https://github.com/getexperimently/experimently/compare/v0.9.0...v0.10.0) (2026-09-28)


### ⚠ BREAKING CHANGES

* **users:** Non-superusers now get 403 when a PUT /api/v1/users/{id} request changes email or username. Administrators change them through /api/v1/admin/users/{id}.

### Features

* **dashboard:** results show the beta notice the API sends ([#314](https://github.com/getexperimently/experimently/issues/314)) ([eceba3f](https://github.com/getexperimently/experimently/commit/eceba3f156324869a3be9389ffda5a6ea22f8c24))
* **infra:** alarms email a required address, and the rollback alarms announce themselves ([#308](https://github.com/getexperimently/experimently/issues/308)) ([9e50305](https://github.com/getexperimently/experimently/commit/9e503053eb8a11044806e171b965c8ec5f403291))
* **infra:** staging runs one small Redis node ([#301](https://github.com/getexperimently/experimently/issues/301)) ([c400adc](https://github.com/getexperimently/experimently/commit/c400adc5a35b853e7c8d5d2101138ee13772a7e4))
* **modules:** a key-list setting and helper for encrypting stored credentials ([#317](https://github.com/getexperimently/experimently/issues/317)) ([4a08a34](https://github.com/getexperimently/experimently/commit/4a08a3487f261de760e8b5a7048d4902c884dbdf))
* **sdk:** assignments carry whether the user was enrolled and why ([#315](https://github.com/getexperimently/experimently/issues/315)) ([c9b3023](https://github.com/getexperimently/experimently/commit/c9b3023e1ef8f38a223032bbb81ecd235275c1a1))


### Bug Fixes

* **api-keys:** only roles that can change flags can create server-side evaluation keys ([#305](https://github.com/getexperimently/experimently/issues/305)) ([3d459e2](https://github.com/getexperimently/experimently/commit/3d459e2cf156fd0776e71d1c0f8724ec00ae9194))
* **config:** the API starts only with an explicit ENVIRONMENT, and a weak first-admin password is refused outside development ([#310](https://github.com/getexperimently/experimently/issues/310)) ([9d27c3f](https://github.com/getexperimently/experimently/commit/9d27c3f95cf8c2acca44a1a61f48f61dc1d232a9))
* **users:** only a superuser can change a user's email or username ([#340](https://github.com/getexperimently/experimently/issues/340)) ([03b0c20](https://github.com/getexperimently/experimently/commit/03b0c2066d41bfeb1ea25720eec7c57362696b0c))

## [0.9.0](https://github.com/getexperimently/experimently/compare/v0.8.0...v0.9.0) (2026-09-28)


### ⚠ BREAKING CHANGES

* **modules:** remove the warehouse endpoints and the ad-hoc ETL query endpoint ([#318](https://github.com/getexperimently/experimently/issues/318))

### Bug Fixes

* **deploy:** workflow AWS sessions last as long as the job can ([#302](https://github.com/getexperimently/experimently/issues/302)) ([cbf1db8](https://github.com/getexperimently/experimently/commit/cbf1db81b7c7a6c85d60e3333a4080b474681467))
* **modules:** remove the warehouse endpoints and the ad-hoc ETL query endpoint ([#318](https://github.com/getexperimently/experimently/issues/318)) ([fdfae20](https://github.com/getexperimently/experimently/commit/fdfae2047f7a48df27a06e2f3bc46c15be071685))

## [0.8.0](https://github.com/getexperimently/experimently/compare/v0.7.0...v0.8.0) (2026-09-27)


### Features

* **chart:** a Helm chart for self-hosting, with render-time checks ([#257](https://github.com/getexperimently/experimently/issues/257)) ([70a5db3](https://github.com/getexperimently/experimently/commit/70a5db39b92f8edc454fec53fc704e05e0092b9c))
* **deploy:** a production compose file that runs the published images ([#252](https://github.com/getexperimently/experimently/issues/252)) ([634da12](https://github.com/getexperimently/experimently/commit/634da12ad9c828498db9fb25f1b73fda0f6298f8))


### Bug Fixes

* **api-keys:** one scope parser, and the dashboard stops offering scopes nothing enforces ([#243](https://github.com/getexperimently/experimently/issues/243)) ([8f4e507](https://github.com/getexperimently/experimently/commit/8f4e507af196866e5fc4c916ced7698b099bf2bb))
* **api:** a rejected Host names the setting that fixes it, and the dashboard explains it ([#274](https://github.com/getexperimently/experimently/issues/274)) ([1eaf65c](https://github.com/getexperimently/experimently/commit/1eaf65c973232d399e8cbba6c6ff5e993713e0e3))
* **api:** Bayesian analysis can be enabled through the experiments API ([#254](https://github.com/getexperimently/experimently/issues/254)) ([24b6215](https://github.com/getexperimently/experimently/commit/24b621580676f2c57d4754479fd3afaba6a9255e))
* **api:** CUPED and interaction analysis say what they compute, and are marked beta ([#280](https://github.com/getexperimently/experimently/issues/280)) ([bc770f0](https://github.com/getexperimently/experimently/commit/bc770f0855296f9a6303252aaf50cdbf006ea000))
* **api:** production start-up lists every missing setting at once ([#277](https://github.com/getexperimently/experimently/issues/277)) ([74371a0](https://github.com/getexperimently/experimently/commit/74371a0f2f6fe13c95f2b52866ed95ed9c10aaad))
* **api:** results breakdowns name each variant and know which is the control ([#276](https://github.com/getexperimently/experimently/issues/276)) ([4b3643e](https://github.com/getexperimently/experimently/commit/4b3643e7e515cb7eeffc2141ef7895543dd8a1a4))
* **api:** sequential analysis honours alpha and stops returning a planned-looks table it does not compute ([#282](https://github.com/getexperimently/experimently/issues/282)) ([860d891](https://github.com/getexperimently/experimently/commit/860d891da7621840eddd02747e6dd4e3f24f9af3))
* **config:** TESTING cannot be combined with a staging or production environment, and the image refuses ENVIRONMENT=test ([#284](https://github.com/getexperimently/experimently/issues/284)) ([c885f02](https://github.com/getexperimently/experimently/commit/c885f023fbba1d9c7f2c7a7a63b8c08078465a38))
* **db:** an older image meeting a newer database says so, and never advises deleting alembic_version rows ([#250](https://github.com/getexperimently/experimently/issues/250)) ([2232cc2](https://github.com/getexperimently/experimently/commit/2232cc29b33528e03d602257cb695ecbc2722ff2))
* **deploy:** a transient AWS error no longer ends a rollback mid-wait ([#215](https://github.com/getexperimently/experimently/issues/215)) ([#224](https://github.com/getexperimently/experimently/issues/224)) ([5944322](https://github.com/getexperimently/experimently/commit/5944322b2b40b272973bb59aa1c15c94d72c202d))
* **deps:** the API image includes the Anthropic and OpenAI SDKs its LLM features import ([#253](https://github.com/getexperimently/experimently/issues/253)) ([8059755](https://github.com/getexperimently/experimently/commit/80597554d95c8538a57aeb900d142a16881f7fec))
* **infra:** Aurora uses an orderable engine version and valid memory settings ([#300](https://github.com/getexperimently/experimently/issues/300)) ([71932b4](https://github.com/getexperimently/experimently/commit/71932b40c17078778a652e8411ca88f98996f2cb))
* **llm:** the Gemini provider calls the REST API instead of a package the image does not ship ([#275](https://github.com/getexperimently/experimently/issues/275)) ([b1db69f](https://github.com/getexperimently/experimently/commit/b1db69fdad1ffe761448d7f5e6d559a8ba9f5057))
* **redis:** every Redis client uses REDIS_PASSWORD ([#248](https://github.com/getexperimently/experimently/issues/248)) ([76cca1a](https://github.com/getexperimently/experimently/commit/76cca1ad8aa40442f56f22ed611bfe04977a709f))
* **release:** release notes and image labels describe what is actually published ([#246](https://github.com/getexperimently/experimently/issues/246)) ([eb6cc53](https://github.com/getexperimently/experimently/commit/eb6cc5362c83a95d24b831e34b20a9d5a323e12c))
* **rules:** evaluation no longer logs user attribute values ([#273](https://github.com/getexperimently/experimently/issues/273)) ([5e687d8](https://github.com/getexperimently/experimently/commit/5e687d8be2b14edbadd8e42e7ac68af60a68ed67))
* **safety:** count errors in the database, and time and size-bound client error reports ([#306](https://github.com/getexperimently/experimently/issues/306)) ([fd9c419](https://github.com/getexperimently/experimently/commit/fd9c419ecdc7c14e21b52527ac251d4f736b65f0))
* **seed:** demo seeds run only in development and test ([#249](https://github.com/getexperimently/experimently/issues/249)) ([c8ecdc3](https://github.com/getexperimently/experimently/commit/c8ecdc3bab8acfb2a512cafb65fe7e7437e0fd72))
* **test:** the data seeder fails loudly and reports what the platform stored ([#245](https://github.com/getexperimently/experimently/issues/245)) ([6df9e10](https://github.com/getexperimently/experimently/commit/6df9e1017df462298379464423742954f9889a9f))
* **workspaces:** enforce owner-only role changes and scope key operations to their workspace ([#266](https://github.com/getexperimently/experimently/issues/266)) ([9454dde](https://github.com/getexperimently/experimently/commit/9454ddee827da51fd94918bcf183ea9ebf0f8691))


### Documentation

* **workspaces:** describe workspaces as grouping, not an access boundary ([#262](https://github.com/getexperimently/experimently/issues/262)) ([a350ba2](https://github.com/getexperimently/experimently/commit/a350ba20e429ba453895f1ca6cfeb236eb46d77f))

## [0.7.0](https://github.com/getexperimently/experimently/compare/v0.6.0...v0.7.0) (2026-09-27)


### ⚠ BREAKING CHANGES

* **rules:** regex targeting conditions (`regex` in the dashboard, `match_regex` in the rules engine) now use RE2 syntax (https://github.com/google/re2/wiki/Syntax), which differs from Python's:
    - `\w`, `\d`, `\s` and `\b` match ASCII only.
    - `$` matches only at the very end of the value, not before a final
    newline.
    - These are refused: lookahead and lookbehind, backreferences, `\Z`,
    `(?x)`, `(?u)`, `(?a)`, `\N{...}`, counted repetitions over 1000, and
    very large Unicode repetitions such as `[\p{L}\p{N}]{1,300}`.
    - Values longer than 256 characters are not evaluated.

    When a stored pattern is refused, or a value is longer than 256 characters
    or cannot be encoded as UTF-8, the rules holding it are not applied: the
    feature flag evaluates disabled with reason "error" (no fall-through to its
    rollout percentage), the user is ineligible for the experiment and gets the
    control variant, and the user is not a member of the segment.

    After upgrading, run `python -m backend.scripts.check_regex_rules` to list
    stored patterns that RE2 refuses or that use `\w`, `\d`, `\s`, `\b` or `$`.

### Bug Fixes

* **api:** export endpoints share one limit of 10 requests a minute per client ([#261](https://github.com/getexperimently/experimently/issues/261)) ([00870e4](https://github.com/getexperimently/experimently/commit/00870e4870ff3e13e0a2ac2265f5ba5e4f8efb84))
* **api:** exports carry the same results as the results API, and the report honours format=csv ([#259](https://github.com/getexperimently/experimently/issues/259)) ([c11a4a0](https://github.com/getexperimently/experimently/commit/c11a4a04415a4d23f50dec796f1ac757e27b8648))
* **rules:** regex conditions use RE2 syntax ([#260](https://github.com/getexperimently/experimently/issues/260)) ([5a3c940](https://github.com/getexperimently/experimently/commit/5a3c940dae073e377fe994bcd628c6436220e435))
* **segments:** audience preview rejects oversized rulesets ([#268](https://github.com/getexperimently/experimently/issues/268)) ([b654656](https://github.com/getexperimently/experimently/commit/b6546563856cf511173c292d751ce8247b5d2c1b))

## [0.6.0](https://github.com/getexperimently/experimently/compare/v0.5.0...v0.6.0) (2026-09-27)


### Features

* **deploy:** an ECS rolling rollout script that cannot mistake a circuit-breaker rollback for success ([#69](https://github.com/getexperimently/experimently/issues/69)) ([#172](https://github.com/getexperimently/experimently/issues/172)) ([38e8d14](https://github.com/getexperimently/experimently/commit/38e8d14e11990a6969157f2a2bb7e46570270fe2))
* **docs:** every documentation page with a shell example is under the runner's contract (E0a-1) ([#179](https://github.com/getexperimently/experimently/issues/179)) ([b16c2ab](https://github.com/getexperimently/experimently/commit/b16c2ab4d5dc646df3b87b800d1c709d0c270d0b))


### Bug Fixes

* **api:** audit log reads follow the role table ([#258](https://github.com/getexperimently/experimently/issues/258)) ([c1f559a](https://github.com/getexperimently/experimently/commit/c1f559a3572a49b332786b050968671ae6b3ef11))
* **api:** experiments report their bandit optimization type ([#197](https://github.com/getexperimently/experimently/issues/197)) ([#204](https://github.com/getexperimently/experimently/issues/204)) ([5ef8513](https://github.com/getexperimently/experimently/commit/5ef851324a8f347975cd2c38a3fdb27f5268ed6f))
* **api:** record when an API key was last used ([#198](https://github.com/getexperimently/experimently/issues/198)) ([#206](https://github.com/getexperimently/experimently/issues/206)) ([14d42ec](https://github.com/getexperimently/experimently/commit/14d42ec182c1b150744fb34c19b4e029769399fe))
* **deploy:** a crash in the serving check reads as "could not tell", not "not yet" ([#182](https://github.com/getexperimently/experimently/issues/182)) ([827671c](https://github.com/getexperimently/experimently/commit/827671ca12709ff8418ccd8b1118bbfb4e8ff88a))
* **deploy:** Rollback never reports success it did not achieve, and never stops CodeDeploy's own rollback ([#148](https://github.com/getexperimently/experimently/issues/148) PR-1) ([#214](https://github.com/getexperimently/experimently/issues/214)) ([18cf907](https://github.com/getexperimently/experimently/commit/18cf907559054e4357202c00713d067e16483869))


### Documentation

* deployment examples that do the wrong thing when pasted into zsh ([#98](https://github.com/getexperimently/experimently/issues/98), part) ([#187](https://github.com/getexperimently/experimently/issues/187)) ([ce5e7ad](https://github.com/getexperimently/experimently/commit/ce5e7ad63e66a5e15f8ff60c74314b197406ddbe))
* more API pages run their examples (Stream E batch 4) ([#223](https://github.com/getexperimently/experimently/issues/223)) ([dbad65e](https://github.com/getexperimently/experimently/commit/dbad65e59a02cf50541c7b27f53caef033156ff3))
* replace the internal incident-response plan with a short page for self-hosters ([#189](https://github.com/getexperimently/experimently/issues/189)) ([bf8ea56](https://github.com/getexperimently/experimently/commit/bf8ea560fe7ea7d8536bc7fea716ccb11a948ac3))
* the demo READMEs are under the documentation checks (Stream E batch 2) ([#194](https://github.com/getexperimently/experimently/issues/194)) ([4ca6b79](https://github.com/getexperimently/experimently/commit/4ca6b799f13036ea30b2690cb1b5055277e01fbf))
* the first integration pages run their examples (Stream E batch 3) ([#199](https://github.com/getexperimently/experimently/issues/199)) ([3b63f03](https://github.com/getexperimently/experimently/commit/3b63f033fd32f2787ee756b5a0d7644ac1d16af2))
* the front-door pages run their examples (Stream E batch 1) ([#188](https://github.com/getexperimently/experimently/issues/188)) ([29d2a8c](https://github.com/getexperimently/experimently/commit/29d2a8cd29c665b767241e27810edc9d27ba39b1))

## [0.5.0](https://github.com/getexperimently/experimently/compare/v0.4.0...v0.5.0) (2026-09-26)


### Features

* **deploy:** check and pin the dashboard's running digest before cdk deploy ([#69](https://github.com/getexperimently/experimently/issues/69)) ([#170](https://github.com/getexperimently/experimently/issues/170)) ([837cc4f](https://github.com/getexperimently/experimently/commit/837cc4fb13ffe216ffcf571e1e71a18edf06ef46))
* **deploy:** deploy, migrate and roll back staging or prod from one path ([#138](https://github.com/getexperimently/experimently/issues/138), [#71](https://github.com/getexperimently/experimently/issues/71), [#70](https://github.com/getexperimently/experimently/issues/70), [#73](https://github.com/getexperimently/experimently/issues/73), [#140](https://github.com/getexperimently/experimently/issues/140), [#144](https://github.com/getexperimently/experimently/issues/144)) ([#160](https://github.com/getexperimently/experimently/issues/160)) ([d23964c](https://github.com/getexperimently/experimently/commit/d23964c9f41e2ad28d8a8b21f4afbc5409770ef7))
* **deploy:** the forward deploy shifts traffic and completes ([#143](https://github.com/getexperimently/experimently/issues/143)) ([#171](https://github.com/getexperimently/experimently/issues/171)) ([dbaadd2](https://github.com/getexperimently/experimently/commit/dbaadd2ea1190e37ccba197b095a6586b604b71f))


### Documentation

* close the two code fences left open at end of file ([#95](https://github.com/getexperimently/experimently/issues/95), [#96](https://github.com/getexperimently/experimently/issues/96)) ([#163](https://github.com/getexperimently/experimently/issues/163)) ([9ac7513](https://github.com/getexperimently/experimently/commit/9ac7513b998fef9fc389b0db58c2d766421c0ad9))
* example lines that do the wrong thing when pasted into zsh ([#98](https://github.com/getexperimently/experimently/issues/98), part) ([#176](https://github.com/getexperimently/experimently/issues/176)) ([1e48fe6](https://github.com/getexperimently/experimently/commit/1e48fe685e19fe3cb932a34f24164d14531260a4))
* name the language of every unlabelled code fence (Stream E pre) ([#175](https://github.com/getexperimently/experimently/issues/175)) ([2e32527](https://github.com/getexperimently/experimently/commit/2e32527604e4fd76d04e61b11ef7bc2f913ffd0c))
* remove the page about the maintainers' agent tooling ([#174](https://github.com/getexperimently/experimently/issues/174)) ([000afce](https://github.com/getexperimently/experimently/commit/000afced384d59542c54fee97a97cddac42d1db7))
* remove the Route 53 page, which describes hosting the marketing site ([#173](https://github.com/getexperimently/experimently/issues/173)) ([d782a46](https://github.com/getexperimently/experimently/commit/d782a46e97f834db8d32566d269a41b3c7f1e62b))
* say which packages are not published yet, and how to install from source ([#177](https://github.com/getexperimently/experimently/issues/177)) ([82c4511](https://github.com/getexperimently/experimently/commit/82c4511f9dc838f691483dd2af60143d3a591aac))
* the CDK runs the dashboard service; correct the pages that say it does not ([#69](https://github.com/getexperimently/experimently/issues/69), [#167](https://github.com/getexperimently/experimently/issues/167)) ([#169](https://github.com/getexperimently/experimently/issues/169)) ([a14ba38](https://github.com/getexperimently/experimently/commit/a14ba3864b5936a60395ccf4aa4841562c51bffb))

## [0.4.0](https://github.com/getexperimently/experimently/compare/v0.3.0...v0.4.0) (2026-09-26)


### ⚠ BREAKING CHANGES

* **infra:** an environment deployed before this change cannot be updated in place -- the ECS cluster rename changes an export the fargate stack imports, and CloudFormation refuses that. For each such environment: `cdk destroy --exclusively experimentation-fargate-<env>`, delete the log groups /ecs/experimentation-backend-<env> and /ecs/experimentation-dashboard-<env> that the old template retained, then `cdk deploy experimentation-compute-<env>`, `cdk deploy experimentation-fargate-<env>`, and `cdk deploy --all`. The analytics stack is not destroyed. See docs/self-hosting/cdk.md, "Moving an environment deployed before the rename".

### Features

* **infra:** dashboard ECS service and ALB path routing ([#69](https://github.com/getexperimently/experimently/issues/69)) ([#145](https://github.com/getexperimently/experimently/issues/145)) ([cdd8abc](https://github.com/getexperimently/experimently/commit/cdd8abce5bfdb74bbbd88a75adef2e05466a2b28))


### Bug Fixes

* **api:** the feature-flag list and detail no longer return 500 under production settings ([#152](https://github.com/getexperimently/experimently/issues/152)) ([d74dd4b](https://github.com/getexperimently/experimently/commit/d74dd4b85bf85e11add30fe391fcfc647f20f0af))
* **infra:** deployed tasks reach Aurora ([#78](https://github.com/getexperimently/experimently/issues/78), [#146](https://github.com/getexperimently/experimently/issues/146)) ([#156](https://github.com/getexperimently/experimently/issues/156)) ([8e2df8a](https://github.com/getexperimently/experimently/commit/8e2df8ac29db07f1990bf7fb518ce4474dd15273))
* **infra:** deployed tasks reach Redis over TLS ([#147](https://github.com/getexperimently/experimently/issues/147)) ([#159](https://github.com/getexperimently/experimently/issues/159)) ([1c32bbc](https://github.com/getexperimently/experimently/commit/1c32bbc40cd8b545a77bbd58bd67d66703b68458))
* **infra:** environments neither collide nor leave billed residue ([#139](https://github.com/getexperimently/experimently/issues/139), [#142](https://github.com/getexperimently/experimently/issues/142)) ([#157](https://github.com/getexperimently/experimently/issues/157)) ([3519958](https://github.com/getexperimently/experimently/commit/35199583b01c22a3b5089572ed13f257c2041a7c))
* **sso:** org_domain is normalised on write; docs/auth/sso.md describes the SSO that exists ([#151](https://github.com/getexperimently/experimently/issues/151)) ([9ae7ebd](https://github.com/getexperimently/experimently/commit/9ae7ebd3b0556ce7a0ea6edf8e7e07cbb7ea1d3f))

## [0.3.0](https://github.com/getexperimently/experimently/compare/v0.2.7...v0.3.0) (2026-09-26)


### Features

* **sso:** sign in with SSO from the dashboard ([#66](https://github.com/getexperimently/experimently/issues/66)) ([#135](https://github.com/getexperimently/experimently/issues/135)) ([aba5780](https://github.com/getexperimently/experimently/commit/aba57803f66033d2c8e390719bc2dcb8dadde4e5))


### Bug Fixes

* **sso:** accept email_verified sent as the string "true" ([#134](https://github.com/getexperimently/experimently/issues/134)) ([518eb93](https://github.com/getexperimently/experimently/commit/518eb9352d8a6c746f8611035ef6e7c3bcbe9958))

## [0.2.7](https://github.com/getexperimently/experimently/compare/v0.2.6...v0.2.7) (2026-09-26)


### Bug Fixes

* **api:** CORS origins are normalised, so BACKEND_CORS_ORIGINS matches ([#126](https://github.com/getexperimently/experimently/issues/126)) ([#132](https://github.com/getexperimently/experimently/issues/132)) ([db54023](https://github.com/getexperimently/experimently/commit/db540235582f8c747773da8392c28cdf2e205f3d))
* **sso:** OIDC sign-in bound to the browser that started it, with PKCE and a verified email ([#66](https://github.com/getexperimently/experimently/issues/66)) ([#129](https://github.com/getexperimently/experimently/issues/129)) ([0274a21](https://github.com/getexperimently/experimently/commit/0274a21d5dc9476180fda57a22922c5401eb65c6))

## [0.2.6](https://github.com/getexperimently/experimently/compare/v0.2.5...v0.2.6) (2026-09-26)


### Bug Fixes

* **api:** unhandled 500s carry CORS and request-id headers ([#72](https://github.com/getexperimently/experimently/issues/72)) ([#118](https://github.com/getexperimently/experimently/issues/118)) ([ab18a43](https://github.com/getexperimently/experimently/commit/ab18a433ed0df33f6e3a0b16fc0f90fe75304f92))
* **dashboard:** say "server error" with a request id, not "can't reach the API" ([#117](https://github.com/getexperimently/experimently/issues/117)) ([beffdf7](https://github.com/getexperimently/experimently/commit/beffdf79b36f042876face40f539d29cf56a9c6b))
* **sso:** signing in no longer changes an existing account's role or provisions outside the configured domain ([#128](https://github.com/getexperimently/experimently/issues/128)) ([32ecbab](https://github.com/getexperimently/experimently/commit/32ecbabf098c495b4bcf5d0612c4a4c460ab3205))
* **sso:** token-exchange and userinfo errors no longer echo provider or connection text ([#116](https://github.com/getexperimently/experimently/issues/116)) ([b8ba9ec](https://github.com/getexperimently/experimently/commit/b8ba9ecf03de8c303adec5d8de1eee6702217f71))


### Documentation

* **deploy:** describe the AWS deployment that exists ([#69](https://github.com/getexperimently/experimently/issues/69)) ([#124](https://github.com/getexperimently/experimently/issues/124)) ([ddf2c0b](https://github.com/getexperimently/experimently/commit/ddf2c0b20c1e2a232ae1d56bcd190745363c8e73))

## [0.2.5](https://github.com/getexperimently/experimently/compare/v0.2.4...v0.2.5) (2026-09-25)


### Bug Fixes

* **docs:** feature-flags/create.md describes the real flag contract ([#103](https://github.com/getexperimently/experimently/issues/103)) ([d0fb6d7](https://github.com/getexperimently/experimently/commit/d0fb6d73e9c7dbb1bb10109d2680401b33245744))
* **docs:** the quick-start runs as written, and pastes into zsh ([#99](https://github.com/getexperimently/experimently/issues/99)) ([d77c18e](https://github.com/getexperimently/experimently/commit/d77c18e9d9f76b4469ba42f61d29034492175c21))


### Documentation

* **feature-flags:** describe flag access by role, not ownership ([#104](https://github.com/getexperimently/experimently/issues/104)) ([d99c154](https://github.com/getexperimently/experimently/commit/d99c1545887d872aaaee06fc5e7b500ca4732da5))
* plan, review and sign-off before building ([#89](https://github.com/getexperimently/experimently/issues/89)) ([399bcce](https://github.com/getexperimently/experimently/commit/399bccedd9acb34765643b9ed69e14788126f287))

## [0.2.4](https://github.com/getexperimently/experimently/compare/v0.2.3...v0.2.4) (2026-09-25)


### Bug Fixes

* **feature-flags:** record the creator as owner and apply one access rule to every flag change ([#101](https://github.com/getexperimently/experimently/issues/101)) ([1fbd549](https://github.com/getexperimently/experimently/commit/1fbd5495d36b8bc7d68825f130414f17e3abc43c))

## [0.2.3](https://github.com/getexperimently/experimently/compare/v0.2.2...v0.2.3) (2026-09-25)


### Bug Fixes

* **security:** refuse a request whose Host header is not one of ours ([#61](https://github.com/getexperimently/experimently/issues/61)) ([2970ff7](https://github.com/getexperimently/experimently/commit/2970ff7a5aa95901983dde2cd8de8f33e3ae7d87))
* **security:** stop trusting every peer's X-Forwarded-* headers ([#62](https://github.com/getexperimently/experimently/issues/62)) ([db82ecc](https://github.com/getexperimently/experimently/commit/db82eccb2bdc3fdcff675893bc2071fc71e2cf13))
* **sso:** bound the OIDC state store ([#63](https://github.com/getexperimently/experimently/issues/63)) ([8764277](https://github.com/getexperimently/experimently/commit/876427705565367406a17c865ba8d92c11f32c08))
* **sso:** build the OIDC redirect_uri from configuration, not the request ([#59](https://github.com/getexperimently/experimently/issues/59)) ([e7a2f03](https://github.com/getexperimently/experimently/commit/e7a2f03bf55d304518ef7b46e481d5c8240cb8c5))


### Documentation

* the public changelog ([e5abf96](https://github.com/getexperimently/experimently/commit/e5abf96a182dd6cce3e0c765235b980fdeb0bea9))

## 0.2.0 — 2026-09-24

The first public release.

### Assignment now agrees across every implementation

The API and the Lambda assigned the same user to different variants. Both now
use one consistent hash — MD5 of `{user_id}:{key}`, first four bytes read
little-endian, divided by 2^32 — pinned to the golden vectors in
`tests/sdk-contract/golden-vectors.json` that the SDKs are tested against.
Verified over 2,000 users with zero differing buckets.

If you ran an earlier build, **assignments may change on upgrade** for users
the two paths disagreed about. Results gathered before and after are not
directly comparable for an experiment that was live across the upgrade.

### Security

- `gunicorn` 21.2.0 → 22.0.0, closing two request-smuggling advisories.
- Cleared the critical `handlebars` advisory and every high-severity one in
  the SDK lockfiles.
- Two security groups that were open to the whole internet are closed.
- The experiments list endpoint enforces the LIST permission.

### Deployment

- The CDK dependency cycle is broken, and a real `cdk synth` runs on every
  pull request rather than a stubbed one.
- Rollbacks go through CodeDeploy, and a traffic shift is approved rather than
  assumed.
- Resource names are deterministic and consistent across trees — the ECS
  cluster, the ECR repository and one Glue catalog instead of two.
- The published images no longer include an arm64 build that could not run.

### Dashboard

- A public homepage at `/`, with every dashboard route still behind
  authentication.
- A rate-limited login now reads as a rate-limited login rather than a dead
  API.

### Documentation

Every documentation link in the dashboard pointed at a site that had never
been built. The docs now resolve, and the tree they point into has been
checked: no broken links or anchors, no API endpoint that does not exist, and
no code example importing something that is not there.

## 0.1.0

Internal only; never published.
