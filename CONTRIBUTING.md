# Contributing to Controlvar

Thanks for wanting to help. This document is the short version of everything you
need: how to sign your work, how to run the stack, and where code lives.

## Before you start

- **Bugs and features**: open an issue first for anything non-trivial, so we can
  agree on the shape before you spend an evening on it. Small fixes — a typo, a
  wrong URL, a one-line bug — can go straight to a pull request.
- **Security problems**: do **not** open an issue. Follow
  [SECURITY.md](SECURITY.md). A public issue for a vulnerability is itself the
  disclosure.
- **Conduct**: by participating you agree to the
  [Code of Conduct](CODE_OF_CONDUCT.md).

## Developer Certificate of Origin (DCO)

Controlvar uses the [Developer Certificate of Origin](https://developercertificate.org/),
not a CLA. You keep the copyright in your contribution; you certify that you
have the right to submit it under the licence of the file you are changing.
There is no paperwork and no copyright assignment, anywhere in the tree.

Sign off every commit:

```{.bash skip reason="dev: commits to your branch"}
git commit -s -m "Fix flag evaluation for empty targeting rules"
```

`-s` appends a line to the commit message:

```text
Signed-off-by: Your Name <your.email@example.com>
```

It must match the author of the commit. To set it up once, with your own name and address:

```{.bash skip reason="dev: changes this checkout's git identity"}
git config user.name  "Your Name"
git config user.email "your.email@example.com"
```

To sign off a branch you already wrote:

```{.bash skip reason="dev: rewrites your branch"}
git rebase --signoff main
```

A pull request with unsigned commits fails the **DCO** check
(`.github/workflows/dco.yml`), which reports on every pull request. **DCO** is
a required status check in `main`'s branch protection, so a pull request with
an unsigned commit cannot merge until every commit carries a sign-off.

A sign-off matching the commit's **author or its committer** is accepted, which
is what the DCO GitHub App does too — so `git rebase --signoff` works even on a
commit you did not author. Commits from Dependabot and github-actions are
exempt: a bot cannot certify the origin of anything, and the human who merges
the pull request is the one taking responsibility.

**Licence of your contribution.** Everything except `sdk/` — the backend, the
optional modules under `modules/`, the dashboard, the docs — is
[Apache-2.0](LICENSE), and a contribution to any of it is made under Apache-2.0
(section 5 of the licence says exactly that). A contribution to `sdk/` is made
under [MIT](sdk/LICENSE). The `LICENSE` file covering the directory you touch is
the licence you are contributing under.

## Running the stack

Everything runs from the repository root. Prerequisites: Python 3.11, Node 22
(see `.nvmrc`), Docker.

`make venv install` creates the virtualenv and installs the backend dependencies,
then the frontend's. `make dev` starts Postgres and Redis, bootstraps the schema
and the first admin, then runs the API on :8000 with reload:

```{.bash skip reason="server: installs the dependencies, then runs the API with reload"}
make venv install
make dev
```

In a second shell, `make web` serves the dashboard on :3100:

```{.bash skip reason="server: serves the dashboard until you stop it"}
make web
```

`make dev` runs `ENVIRONMENT=development AUTH_PROVIDER=local uvicorn backend.app.main:app`
after `docker compose up -d --wait postgres redis` and
`ENVIRONMENT=development python -m backend.app.db.bootstrap`. The API does not
start without `ENVIRONMENT`. Bootstrap is idempotent: it creates the
schema from the models and stamps the alembic heads on a fresh database, and runs
`alembic upgrade heads` on an existing one. Do not run `alembic upgrade heads` on
an empty database — the historical migration chain cannot replay from nothing.

`heads` is plural throughout: a full checkout has two — the core chain and the
`modules` branch, kept separate so that deleting `modules/` leaves a consistent
core chain. Do not merge them; a new revision names the head it extends
(`alembic revision --autogenerate --head modules@head -m "..."`).

**Expand, then contract.** A migration in release N must not drop or rename
anything release N-1's code uses; do it in two releases. Add the new column or
table and move the code over in one release, and drop the old one in a later
release. During a rolling upgrade release N-1 keeps serving against release N's
schema after N's migration has run, and an older image that meets a newer
database refuses to start rather than guess (see "An Older Image Against a
Newer Database" in `docs/self-hosting/migrations.md`).

Other useful targets — `make help` lists them all:

| Target | What it does |
|---|---|
| `make up` | The core stack in Docker (API, dashboard, Postgres, Redis) |
| `make demo` | The whole stack including the ShopLab and StreamPulse demo apps |
| `make down` | Stop everything and drop the volumes |
| `make bootstrap` | Create the schema and the first administrator |
| `make openapi` | Regenerate the OpenAPI fixtures: the frontend URL guard's and the two stable snapshots |
| `make core-build` | Prove the core profile stands alone (copy, delete `modules/`, rebuild, test) |
| `make full-build` | The same sequence on the full tree, `modules/` included |
| `make lock` | Regenerate both requirements locks (what the two API images install) |

### Tests

| Target | What it runs |
|---|---|
| `make test` | What a pull request must pass: backend + frontend |
| `make test-unit` | Backend unit tests (most need Postgres too) |
| `make test-integration` | Backend integration tests (needs Postgres on localhost:5432) |
| `make test-modules` | The modules' own suite (needs `modules/requirements.txt` installed) |
| `make test-frontend` | Dashboard jest tests, `tsc --noEmit`, production build |
| `make test-sdk` | Cross-SDK golden-vector contract tests |

Both suites need PostgreSQL on **localhost:5432**. `make db` starts it. The unit
suite is not database-free despite the name: 26 of its files take the
`db_session` fixture, and `backend/tests/conftest.py` connects to
`POSTGRES_SERVER` (or `POSTGRES_HOST`) and `POSTGRES_PORT`, defaulting to
`localhost:5432`. The split is by what the test
exercises, not by whether it touches a database.
Run backend tests from the repository root; `pyproject.toml` is the single pytest
configuration and `testpaths` covers `backend/tests` and `modules/backend/tests`.

The test shell, if you run pytest by hand rather than through `make`:

```{.bash skip reason="dev: runs the backend unit tests in your virtualenv"}
source venv/bin/activate
export APP_ENV=test TESTING=true POSTGRES_SCHEMA=test_experimentation
python -m pytest backend/tests/unit -p no:cov -q
```

`POSTGRES_SCHEMA` matters more than it looks: the models bake the schema name
into index names and foreign-key targets at import time, and alembic reflects
whatever `POSTGRES_SCHEMA` names. The two must agree or `alembic revision
--autogenerate` compares one schema's models against another schema's tables
and proposes creating -- or dropping -- everything. The same export sequence
applies when generating a migration. First list the heads:

```{.bash skip reason="dev: reads the migration graph in your virtualenv"}
export APP_ENV=test TESTING=true POSTGRES_SCHEMA=test_experimentation
python -m alembic -c backend/app/db/alembic.ini heads
```

It prints two, such as `<core head id> (head)` and
`modules_0002_warehouse_analysis (modules) (head)`.
The core head is the one without `(modules)`. Generate the revision on it, with your own
message in place of `describe the change`:

```{.bash skip reason="dev: writes a migration file"}
CORE_HEAD=$(python -m alembic -c backend/app/db/alembic.ini heads | grep -v '(modules)' | cut -d' ' -f1)
python -m alembic -c backend/app/db/alembic.ini revision --autogenerate --head "$CORE_HEAD" -m "describe the change"
```

For a change to a module, the head is `modules@head` instead of `"$CORE_HEAD"`.

`backend/tests/integration/database/test_autogenerate_is_empty.py` is the
standing check that a freshly bootstrapped database produces an empty
migration; if you touch anything about schemas or reflection, run it.
Tests that need a module installed are marked `@pytest.mark.modules` and are
skipped in a core build.

Every bug fix needs a regression test. The `regression-guard` CI job fails a pull
request labelled `bug` that adds no test.

### Lint

`make lint` is exactly what the `lint` CI job runs. `make format` runs
`ruff format` and `ruff check --fix`, in place:

```{.bash skip reason="dev: runs this repository's linters"}
make lint
make format
```

`make lint` runs `ruff check backend/ modules/ scripts/ tests/acceptance/`, `ruff format --check`,
`lint-imports` (the core/modules import contracts), `reuse lint` (every file
carries a licence: `REUSE.toml` covers the tree by directory, the texts live in
`LICENSES/`), the requirements-lock check, `npm run lint` and `npx tsc --noEmit`
in `frontend/`, plus `hadolint` on the Dockerfiles and `actionlint` on the
workflows when those two are installed (`brew install hadolint actionlint`).
`ruff` replaces black, isort and flake8 — do not add those back. A change to a
pin in `backend/requirements/runtime.txt` needs `make lock` afterwards: the API
image installs the hashed lock, not the loose pins.

### If the credential scan fails

The `Container Security Scan` job scans `backend/`, `modules/` and the four
images it builds for credentials. When a scan finds one, the failed step prints
each finding's path, rule, severity and line, and the job summary lists the
same rows. Nothing else from the file is printed, and the full report stays on
the runner: it is not uploaded as an artifact.

Open the file at that line in your own checkout. The rule says what kind of
value matched. Paths under `/app/backend/` and `/app/modules/backend/` in an
API image are `backend/` and `modules/backend/` in the tree. A dashboard image
path under `/usr/share/nginx/html/` is build output; to see which file it came
from, build and scan the image locally:

```{.bash skip reason="toolchain: needs trivy 0.74.0 and Docker"}
trivy fs --scanners secret --severity HIGH,CRITICAL backend/
docker build -f frontend/Dockerfile --build-arg EXPERIMENTLY_PROFILE=core -t experimently-web:scan-core .
trivy image --scanners secret experimently-web:scan-core
```

There is no ignore file. If a test fixture matches a rule, build the value at
test time or use one the rule does not match. The rule ids are listed in
trivy's [built-in rules](https://github.com/aquasecurity/trivy/blob/v0.74.0/pkg/fanal/secret/builtin-rules.go).

If the value is a real credential, revoke and replace it first. Removing it in
a later commit does not take it out of the branch's history.

### Documentation

The shell examples in the documentation are run in CI as written. A page you add or
change with a shell block has to be tagged and enrolled; see
[Runnable Documentation Examples](docs/development/doc-examples.md), and run
`python scripts/doc_examples.py --check` before you push.

## Releasing

Releases are cut by CI from a tag. [Releases, images and
verification](docs/self-hosting/releases.md) says what a release publishes and
how to check it; this section is how a maintainer cuts one.

1. Merge work to `main` with [conventional commit](https://www.conventionalcommits.org/)
   subjects (`feat:`, `fix:`, `perf:`, `deps:`, `docs:`, `build:`).
2. `release-please` keeps a standing pull request that accumulates them.
   Merging it writes `CHANGELOG.md`, bumps `VERSION` and the manifest, tags the
   merge commit `vX.Y.Z` and opens a GitHub release.
3. `release.yml` then builds, signs and publishes. It is *called* by
   `release-please.yml` rather than triggered by the tag, because a tag pushed
   with the default `GITHUB_TOKEN` does not start another workflow.

A pre-release is cut by pushing the tag by hand, `git tag v1.2.0-rc.1 && git
push origin v1.2.0-rc.1`, which does start `release.yml`. It publishes the
versioned image tags only: `:core` and `:full` stay on the newest final release,
and a tag with a `-` in it does not deploy the documentation site.

### When a release's image scan fails

Each image job scans its image with trivy's secret scanner before anything is
pushed. When a scan finds something, the job's summary lists each finding's
file path in the image, rule, severity and line; the matched text is never
printed, because the log of a public repository's run is public.

1. **Know what was published.** The failed image was not pushed. The other
   image jobs may have pushed their versioned tags, because each image is
   built separately. No SBOMs or chart are attached to the GitHub release,
   and `:core` and `:full` stay on the previous release. The git tag exists,
   and release-please may already have opened the GitHub release.
2. **Revoke and replace the secret first.** An image is built from the tagged
   tree, so a secret in the image is almost certainly in the repository's
   public history too. Removing the file does not undo that.
3. **Remove the file from the image and cut a new patch release.**
   Re-running the release for the same tag rebuilds the same tree and fails
   the same way.
4. **There is no allow-list.** The workflow passes trivy its own empty
   ignore file, so a finding is fixed by taking the file out of the image,
   never by listing it.
5. **Versioned tags already pushed for the failed version stay** until an
   organization owner deletes those package versions in GitHub Packages. That
   is a manual step.

If "The pushed image is the scanned image" fails instead, the pushed layers
differ from the scanned ones. That image's versioned tag has already been
pushed, but it is not signed and nothing else in the release continues. An
organization owner deletes that package version, and the release is then
re-run for the same tag: `gh workflow run release.yml -f tag=vX.Y.Z`.

### SDK releases

Each SDK is released by its own tag, `sdk/<name>/vX.Y.Z`, through
`sdk-release.yml`. An SDK with no registry account yet is listed as `unwired`
in `scripts/check_sdk_version.py`, and tagging one is refused rather than
quietly publishing nothing. Write a Python version in its PEP 440 spelling
(`1.0.0rc1`, not `1.0.0-rc.1`), in the manifest and the tag alike; the build
job refuses any other spelling. Publish `sdk/js` before `sdk/openfeature`: the
provider's release refuses until the matching `@getexperimently/js-sdk` is on
npm. If a publish job fails after approval, use **Re-run failed jobs**, not
**Re-run all jobs**.

## Layer map

```text
backend/app/        FastAPI application: api/, core/, services/, models/, schemas/
backend/lambda/     Lambda handlers (event processor, flag evaluation), deployed by no stack
backend/scripts/    One-off and seed scripts
backend/tests/      unit/ integration/ smoke/ e2e/ contract/ performance/ realistic/
frontend/           Next.js dashboard (TypeScript)
sdk/<lang>/         16 client SDKs — independent packages, MIT licensed
modules/            The optional modules (workspaces, rbac, sso, hipaa, compliance,
                    warehouse, integrations, counters, etl, split_url) — Apache-2.0,
                    contributions welcome like anywhere else
infrastructure/     AWS CDK stacks
demo/               ShopLab and StreamPulse demo applications
docs/               All documentation, including the modules
tests/sdk-contract/ Cross-SDK golden-vector tests
```

Rules that are easy to get wrong:

- **Backend imports are absolute and fully qualified**:
  `from backend.app.models.metrics.metric import RawMetric`. Never
  `from app.models...` and never a relative import across packages. Two import
  paths to one class make SQLAlchemy treat it as two mapped classes.
- **The entry point is `backend.app.main:app`**, run from the repository root —
  not `app.main:app` from inside `backend/`.
- **Tests are organised by directory, not by marker.** A test that needs no
  database goes in `backend/tests/unit/`; one that needs Postgres goes in
  `backend/tests/integration/`; one that asserts the app is wired correctly goes
  in `backend/tests/smoke/`. Markers exist (`unit`, `integration`, `api`,
  `regression`) but the directory is what CI selects on, and a module-level
  import cannot be skipped by a marker.
- **Nothing in `backend/` may import `modules`.** The core profile must build,
  boot and pass its tests with `modules/` deleted, and three things enforce
  it: `lint-imports` (contracts in `pyproject.toml` `[tool.importlinter]` and
  `backend/lambda/.importlinter`: `backend.app` and the Lambda functions never
  import `modules`; `backend.app` never imports `backend.tests`, `sdk`, `demo`
  or `infrastructure`), `backend/tests/smoke/test_core_boundary.py` (an AST
  scan against the module list in `modules-manifest.txt`, including string
  literals such as `patch()` targets), and `make core-build`
  (`scripts/core_build.sh`), which copies the checkout, deletes `modules/` and
  every manifest path, and then imports, bootstraps, tests, builds and scans
  what is left — the `core-build` CI job runs the same script on every pull
  request, and `full-build` runs the same sequence on the full tree.
  The modules plug into the core through the registration hooks and the
  loader under `backend/app/core/` and `backend/app/modules_loader.py`, never
  the other way round: the dependency arrow points from `modules/` into
  `backend/`, never back. A module that is not installed answers 501 on its
  routes; there is no other gate.
- **The public API has a stable snapshot.** `docs/api/openapi-v1.stable.json`
  (the core profile) and `docs/api/openapi-v1.full.json` (the full profile)
  are compared by `backend/tests/smoke/test_openapi_snapshot.py`: changing the
  shape of a stable route fails the smoke suite. A new or still-changing route
  is marked `openapi_extra={"x-stability": "beta"}` on its decorator, which
  turns the failure into a warning; `make openapi` regenerates the snapshots
  when a change is intended. See `docs/api/stability.md`.
- **SDKs are independent packages.** `sdk/js`, `sdk/python`, `sdk/go` and the
  thirteen others have their own manifests, their own tests and their own
  release pipelines, and they do not import from `backend/`. They share only the
  wire contract and the MD5 consistent-hashing algorithm, which is pinned by the
  golden vectors in `tests/sdk-contract/`. If you change assignment hashing, you
  change all sixteen or none.
- **New SQLAlchemy models must be imported in `backend/app/models/__init__.py`**
  so that standalone scripts can configure the mappers.
- **Pydantic v2 only**: `field_validator`, `model_validator`, `ConfigDict`,
  `pydantic_settings.BaseSettings`.

## Pull requests

1. Fork, then branch from `main` (`git checkout -b fix/flag-eval-empty-rules`).
2. Write the change and a test that fails without it.
3. `make lint && make test`.
4. Commit with `-s`. Conventional-commit prefixes (`fix:`, `feat:`, `docs:`,
   `chore:`) drive the changelog, so please use them.
5. Open the pull request and fill in the template. Describe what breaks without
   the change, not just what you did.

Keep pull requests focused. A refactor and a bug fix in one branch takes three
times as long to review, and a reverted refactor takes the fix with it.
