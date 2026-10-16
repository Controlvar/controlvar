# Controlvar — common tasks.
#
#   make dev        start Postgres + Redis and run the API and dashboard locally
#   make demo       build the images and run the whole stack in Docker
#   make test       the checks a pull request must pass
#
# Everything runs from the repository root; the backend is imported as
# `backend.app.*`, so `uvicorn backend.app.main:app` is the entry point.

SHELL := /bin/bash
VENV := venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/python -m pytest
COMPOSE := docker compose

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: venv
venv: ## Create the virtualenv and install backend dependencies (and the modules' in a full checkout)
	python3.11 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r backend/requirements.txt
	@# One recipe line for guard and command (see test-modules): a core checkout
	@# has no modules/ and must still succeed. Without these, the warehouse and
	@# SSO tests cannot import sqlglot or duckdb and run only in CI.
	@if [ -f modules/requirements.txt ]; then \
		echo '$(PY) -m pip install -r modules/requirements.txt -r modules/requirements-test.txt'; \
		$(PY) -m pip install -r modules/requirements.txt -r modules/requirements-test.txt; \
	else \
		echo "no modules/requirements.txt -- this is a core checkout"; \
	fi

.PHONY: install
install: venv ## Install backend and frontend dependencies
	cd frontend && npm ci

.PHONY: db
db: ## Start Postgres and Redis only
	$(COMPOSE) up -d --wait postgres redis

.PHONY: bootstrap
# ENVIRONMENT is set here as on `dev`: the bootstrap creates a first
# administrator with the development default password only when ENVIRONMENT
# says development (or test), not when it was left unset.
bootstrap: ## Create the schema and the first administrator (idempotent)
	ENVIRONMENT=development $(PY) -m backend.app.db.bootstrap

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

.PHONY: dev
# ENVIRONMENT is declared on the `dev` recipe, not left to the default, so the
# *process* environment says development (a value in .env.dev does not reach
# os.environ, and the configuration validators read os.environ). The API
# refuses to start when ENVIRONMENT is not set at all.
dev: db bootstrap ## Run the API (:8000) with reload; start the dashboard with `make web`
	ENVIRONMENT=development AUTH_PROVIDER=local $(VENV)/bin/uvicorn backend.app.main:app --reload --port 8000

.PHONY: web
web: ## Run the dashboard dev server (:3100, see frontend/package.json)
	cd frontend && npm run dev

.PHONY: demo
demo: ## Build the images and run the full stack, including the demo apps
	SEED=demo,shoplab,streampulse $(COMPOSE) --profile demo up -d --build --wait

.PHONY: up
up: ## Run the core stack in Docker (API, dashboard, Postgres, Redis)
	$(COMPOSE) up -d --build --wait

.PHONY: down
down: ## Stop the stack and drop its volumes
	$(COMPOSE) down -v --remove-orphans

.PHONY: logs
logs: ## Follow the API and dashboard logs
	$(COMPOSE) logs -f api frontend

.PHONY: fuzz
# One API fuzzing pass on this machine, with fuzz.yml's flags, lists and
# verdict, on a compose stack of its own (controlvar-fuzz: its own ports,
# removed again on the way out, image included); scripts/fuzz_local.sh says how
# that stack differs from the workflow's. The seed is FUZZ_SEED, not SEED: SEED is
# docker-compose.yml's data seed, and a variable given to make on the command
# line reaches the recipe's environment.
fuzz: ## Fuzz the API locally: make fuzz FUZZ_SEED=<seed> SHA=<commit> PASS=<superuser|viewer|sdk>
	scripts/fuzz_local.sh "$(FUZZ_SEED)" "$(SHA)" "$(PASS)"

.PHONY: showcase
# A captioned product walkthrough, recorded on this machine and never in CI
# (#1066): tests/acceptance/showcase. It builds the release REF from `git
# archive`, runs its own stack on ports 28400-28402 (Postgres in a throwaway,
# labelled container), captures the storyboard VIDEO and renders it into
# ~/Downloads/experimently-showcase. REF is the release to record (required
# until one has been checked against the storyboards; then it is the default).
# The tool fetches nothing, so `git fetch --tags` first if it is not here.
showcase: ## Record a product walkthrough video locally: make showcase VIDEO=<slug|all> REF=<tag>
	cd tests/acceptance && ../../$(PY) -m showcase.capture "$(VIDEO)" $(if $(REF),--ref "$(REF)",)

.PHONY: showcase-voice
# The narration's clips for the walkthrough videos (#1066): each storyboard
# `say:` sentence spoken by Amazon Polly (generative, Ruth) and slowed to 90%,
# into ~/.cache/experimently-showcase/voice, outside every repository. The only
# target that calls AWS (`make showcase` never does), and only for a sentence
# whose clip is not in that cache yet, with the profile in AWS_PROFILE from your
# own environment: it is never written here. It prints each clip's length and
# level, each video's projected length, and the characters sent.
showcase-voice: ## Make the narration clips with Amazon Polly: AWS_PROFILE=<profile> make showcase-voice VIDEO=<slug|all>
	cd tests/acceptance && ../../$(PY) -m showcase.voice "$(VIDEO)" --voice Ruth

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

.PHONY: test
test: test-backend test-modules test-frontend ## Everything a pull request must pass

.PHONY: test-backend
test-backend: ## Backend unit, integration and smoke tests
	APP_ENV=test TESTING=true $(PYTEST) backend/tests/unit backend/tests/integration backend/tests/smoke -q

.PHONY: test-unit
test-unit: ## Backend unit tests
	APP_ENV=test TESTING=true $(PYTEST) backend/tests/unit -q

.PHONY: test-integration
test-integration: ## Backend integration tests (needs Postgres on localhost:5432)
	APP_ENV=test TESTING=true $(PYTEST) backend/tests/integration -q

.PHONY: test-modules
# The modules carry their own suite and their own conftest, so it is a separate
# pytest session -- and a separate target, because until now the only thing that
# ran it was the 60-minute `full-build`, which is far too slow to tell you that
# a module test broke.
#
# Guard and command are ONE recipe line (a single `if ... fi`, continued with
# backslashes): make runs every recipe line in its own shell and this makefile
# has no `.ONESHELL:`, so a guard written as `test -d ... || { echo; exit 0; }`
# on its own line ends only the guard's shell -- the next line runs anyway.
# That is exactly what happened here: on a core checkout `make test-modules`
# printed the message and then ran pytest against a path that does not exist
# (exit 4), which `test` depends on, so `make test` was red on every core
# checkout.  backend/tests/unit/scripts/test_makefile_guards.py runs both
# targets in a tree with no modules/ and fails on the old shape.
test-modules: ## The modules' own tests (needs modules/, modules/requirements.txt and modules/requirements-test.txt installed)
	@if [ -d modules/backend/tests ]; then \
		echo 'APP_ENV=test TESTING=true $(PYTEST) modules/backend/tests -q'; \
		APP_ENV=test TESTING=true $(PYTEST) modules/backend/tests -q; \
	else \
		echo "no modules/backend/tests -- this is a core checkout"; \
	fi

.PHONY: test-frontend
test-frontend: ## Dashboard tests, type check and production build
	cd frontend && npm test && npx tsc --noEmit && npm run build

.PHONY: test-coverage
test-coverage: ## Backend tests with a coverage report
	APP_ENV=test TESTING=true $(PYTEST) backend/tests/unit backend/tests/integration \
		--cov=backend.app --cov-report=term-missing

.PHONY: test-sdk
test-sdk: ## Each SDK's own hash function against the cross-SDK golden vectors
	$(PYTEST) tests/sdk-contract -q -o addopts=""
	$(VENV)/bin/python tests/sdk-contract/hash_contract.py --list
	$(VENV)/bin/python tests/sdk-contract/hash_contract.py python js edge react-native go

.PHONY: core-build
core-build: $(VENV)/bin/pip-licenses $(VENV)/bin/reuse ## Prove the core profile stands alone: copy the tree, delete modules/, rebuild and test it
	scripts/core_build.sh

.PHONY: full-build
# The whole sequence, locally. CI runs this one with
# `--steps copy,strip,reuse,secrets,import,bootstrap,frontend,licences`
# instead: `--profile full` leaves the tree exactly as the checkout is, so its
# pytest sessions would repeat what the dedicated jobs already ran on the same
# tree. `scripts/core_build.sh --profile full --list-steps` prints a sequence
# without running it.
full-build: $(VENV)/bin/pip-licenses $(VENV)/bin/reuse ## The same sequence on the full tree, modules included
	scripts/core_build.sh --profile full

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

.PHONY: lint
lint: lint-boundary ## Everything the `lint` CI job runs: ruff, import-linter, reuse, lock check, eslint, tsc, hadolint, actionlint
	$(VENV)/bin/ruff check backend/ scripts/ tests/acceptance/ $$(test -d modules && echo modules/)
	$(VENV)/bin/ruff format --check backend/ scripts/ tests/acceptance/ $$(test -d modules && echo modules/)
	cd frontend && npm run lint && npx tsc --noEmit
	@if command -v hadolint >/dev/null; then \
		hadolint backend/Dockerfile frontend/Dockerfile \
			demo/shoplab/Dockerfile demo/streampulse/Dockerfile; \
	else echo "hadolint not installed (brew install hadolint) - skipped"; fi
	@if command -v actionlint >/dev/null; then \
		SHELLCHECK_OPTS='-e SC2086 -e SC2034 -e SC2015 -e SC2046 -e SC2251' actionlint; \
	else echo "actionlint not installed (brew install actionlint) - skipped"; fi

# Boundary tooling, installed into the venv on demand rather than pinned in
# backend/requirements.txt: that file is the product's dependency list and
# feeds THIRD_PARTY_LICENSES.md, and these are repository tools (reuse is
# GPL-3.0-or-later tooling, for one). Same versions as the CI jobs
# (.github/workflows/lint.yml, pr-qa-gate.yml).
IMPORT_LINTER_VERSION := 2.15
REUSE_VERSION := 6.2.0
UV_VERSION := 0.12.13
PIP_LICENSES_VERSION := 5.5.5
$(VENV)/bin/lint-imports:
	$(PY) -m pip install "import-linter==$(IMPORT_LINTER_VERSION)"
$(VENV)/bin/reuse:
	$(PY) -m pip install "reuse==$(REUSE_VERSION)"
$(VENV)/bin/uv:
	$(PY) -m pip install "uv==$(UV_VERSION)"
$(VENV)/bin/pip-licenses:
	$(PY) -m pip install "pip-licenses==$(PIP_LICENSES_VERSION)"

.PHONY: lint-boundary
lint-boundary: $(VENV)/bin/lint-imports $(VENV)/bin/reuse ## The core/modules boundary: import-linter contracts, REUSE licensing, requirements lock, the former name
	$(VENV)/bin/lint-imports
	cd backend/lambda && ../../$(VENV)/bin/lint-imports
	$(VENV)/bin/reuse lint
	@cmp -s LICENSE LICENSES/Apache-2.0.txt \
		|| { echo "LICENSES/Apache-2.0.txt has drifted from LICENSE (cp LICENSE LICENSES/Apache-2.0.txt)"; exit 1; }
	$(PY) scripts/check_requirements_lock.py
	$(PY) scripts/check_legacy_name.py

.PHONY: format
format: ## Format and auto-fix backend/, scripts/, tests/acceptance/ and modules/ in place (ruff replaces black + isort)
	$(VENV)/bin/ruff format backend/ scripts/ tests/acceptance/ $$(test -d modules && echo modules/)
	$(VENV)/bin/ruff check backend/ scripts/ tests/acceptance/ $$(test -d modules && echo modules/) --fix

.PHONY: openapi
# Three fixtures from one tool: the dashboard's URL-guard dump (every route the
# checkout serves, reduced to paths and methods) and the two stable snapshots
# backend/tests/smoke/test_openapi_snapshot.py compares against -- the core
# profile (dumped with the modules package hidden) and the full profile (needs
# modules/ to load).
#
# All three name a profile. The URL guard's fixture is documented as a
# full-profile dump (frontend/src/tests/services/url-literals.test.ts subtracts
# openapi.module-paths.json from it for a core run), but the first line used to
# pass no --profile at all: it dumped whatever the checkout happened to load,
# so regenerating it from a core tree silently rewrote the fixture without the
# modules' 62 paths and the core run then subtracted them a second time.
openapi: ## Regenerate the OpenAPI fixtures: URL guard, core and full snapshots
	$(PY) -m backend.scripts.dump_openapi --profile full frontend/src/tests/fixtures/openapi.json
	$(PY) -m backend.scripts.dump_openapi --profile core --full docs/api/openapi-v1.stable.json
	$(PY) -m backend.scripts.dump_openapi --profile full --full docs/api/openapi-v1.full.json

.PHONY: lock
# uv, not pip-compile: it resolves for every platform at once (--universal), so
# a lock made on macOS is the one the Linux image installs, and it generates
# hashes for every artefact so the Dockerfile can `pip install --require-hashes`.
#
# Two locks, one per profile. The modules lock is resolved with
# backend/requirements.txt as a constraint, so a package both closures contain
# lands on the same version in both -- the full image installs one lock on top
# of the other, and a disagreement would mean the second install silently
# replaced a package the first one resolved.
lock: lock-runtime lock-modules ## Regenerate both requirements locks
	$(PY) scripts/check_requirements_lock.py

.PHONY: lock-runtime
lock-runtime: $(VENV)/bin/uv ## Regenerate backend/requirements/runtime.lock from backend/requirements/runtime.txt
	$(VENV)/bin/uv pip compile backend/requirements/runtime.txt --universal --generate-hashes \
		--python-version 3.11 --custom-compile-command "make lock" \
		-o backend/requirements/runtime.lock

.PHONY: lock-modules
# One recipe line for guard and command, for the reason spelled out on
# `test-modules` above: a `|| { ...; exit 0; }` guard on its own line does not
# stop the next one, so `make lock` used to fail on every core checkout.
lock-modules: $(VENV)/bin/uv ## Regenerate modules/requirements.lock from modules/requirements.txt
	@if [ -f modules/requirements.txt ]; then \
		echo '$(VENV)/bin/uv pip compile modules/requirements.txt ... -o modules/requirements.lock'; \
		$(VENV)/bin/uv pip compile modules/requirements.txt --universal --generate-hashes \
			--python-version 3.11 --constraint backend/requirements.txt \
			--custom-compile-command "make lock" \
			-o modules/requirements.lock; \
	else \
		echo "no modules/requirements.txt -- this is a core checkout"; \
	fi

.PHONY: lock-check
lock-check: ## Fail when a requirements file has a pin its lock does not carry at that version
	$(PY) scripts/check_requirements_lock.py
