# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an AWS-based experimentation platform for A/B testing and feature flags with real-time evaluation capabilities. The platform enables data-driven decisions through robust experimentation and feature management.

## Project Structure

- **backend/**: FastAPI Python services with comprehensive APIs for experiments, feature flags, users, and analytics
- **modules/**: the optional modules — the same layout one level down
  (`modules/backend/app`, `modules/backend/tests`, `modules/frontend/src`,
  `modules/infrastructure/cdk`, `modules/lambda`). Present in a full checkout,
  absent in a core one; `modules-manifest.txt` is the authority on what belongs
  there. Everything under `backend/` must work with the directory deleted.
- **frontend/**: Next.js React application for experiment management and analytics dashboards
- **infrastructure/**: AWS CDK infrastructure definitions for deployment
- **sdk/**: Client SDKs (JavaScript and Python) for integrating with applications
- **docs/**: Comprehensive project documentation
- **scripts/**: repository tooling — `core_build.sh` (prove a profile stands
  alone), the requirements-lock checks, the third-party licence report
- **backend/lambda/**: AWS Lambda functions for real-time operations

## Development Commands

### Backend (Python)

```bash
# Setup environment (from the repository root)
python3.11 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r backend/requirements.txt

# Run development server (the package is backend.app). The API refuses to
# start without ENVIRONMENT (or the legacy APP_ENV).
ENVIRONMENT=development uvicorn backend.app.main:app --reload

# Or use the Makefile: `make db && make dev`

# Testing
pytest  # Run all tests
pytest backend/tests/unit/  # Unit tests only
pytest backend/tests/integration/  # Integration tests only

# Code quality (ruff replaces black, isort and flake8). The scope is
# `backend/ modules/ scripts/ tests/acceptance/` -- exactly what `make format` /
# `make lint` and the lint CI job pass; linting backend/ alone leaves the
# modules, the repository tooling and the acceptance runner unchecked.
ruff format backend/ modules/ scripts/ tests/acceptance/        # Format code
ruff check backend/ modules/ scripts/ tests/acceptance/ --fix   # Lint and auto-fix (includes import sorting)
mypy backend/app/                             # Type checking: NOT gated, NOT clean (#788)
make format                 # The two ruff commands above
make lint                   # Everything the `lint` CI job runs (see below)

# Database migrations. A full checkout has TWO heads -- the core chain and the
# `modules` branch -- so it is always `heads`, and a new revision has to name
# the head it extends (`alembic heads` prints both).
python -m alembic -c backend/app/db/alembic.ini upgrade heads
python -m alembic -c backend/app/db/alembic.ini heads
python -m alembic -c backend/app/db/alembic.ini revision --autogenerate --head <core head id> -m "description"
python -m alembic -c backend/app/db/alembic.ini revision --autogenerate --head modules@head -m "description"
```

### Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev    # Development server
npm test       # Run tests
npm run build  # Production build
```

### Infrastructure (AWS CDK)

```bash
cd infrastructure
cdk bootstrap aws://ACCOUNT-NUMBER/REGION
cdk deploy --all
```

### Docker Development

```bash
# Start local development environment
docker-compose up -d

# View logs
docker-compose logs -f
```

## Architecture

The platform uses a modern microservices architecture:

### Backend Services
- **FastAPI Application**: Main API server with comprehensive endpoints for experiments, feature flags, users, metrics, and safety monitoring
- **Background Schedulers**: Automated systems for experiment scheduling, rollout management, metrics collection, and safety monitoring
- **Lambda Functions**: Real-time services for experiment assignment, feature flag evaluation, and event processing

### Key Backend Components
- **API Layer**: RESTful endpoints organized by resource type (experiments, feature flags, users, etc.)
- **Services Layer**: Business logic for experiments, feature flags, authentication, safety monitoring
- **Data Layer**: SQLAlchemy models with PostgreSQL backend
- **Authentication**: AWS Cognito integration with role-based access control (RBAC)
- **Middleware**: Security headers, request logging, metrics collection, error tracking

### Database Architecture
- **Primary Database**: PostgreSQL (Aurora) for core application data
- **Caching**: Redis (ElastiCache) for session management and caching
- **Analytics**: Kinesis streams with Lambda processors feeding into OpenSearch
- **Time-series Data**: Specialized storage for metrics and events

### Frontend Architecture
- **Next.js**: React-based dashboard for experiment and feature flag management
- **Components**: Reusable UI components for experiments, feature flags, and analytics
- **API Integration**: TypeScript client services for backend communication

## Development Guidelines

### Python/Backend Standards
- Use Python 3.11+ with type annotations
- Follow PEP 8 style guidelines
- Use Pydantic v2 patterns (field_validator, model_validator, ConfigDict)
- Import with full paths: `from backend.app.models.metrics.metric import RawMetric`
- Never use relative imports like `from app.models...`

### Database Operations
- Use SQLAlchemy for all database operations
- Include schema in table definitions
- Use Alembic for migrations: `alembic revision --autogenerate --head <head> -m "description"`,
  where `<head>` is the core head id from `alembic heads` for a core change and `modules@head`
  for a module's. A full checkout has two heads, so a bare `--autogenerate` fails with
  "Multiple heads are present"; alembic puts the new file beside the head it extends.
- Set proper environment variables: `POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation`
- **Fresh database?** Run `ENVIRONMENT=development python -m backend.app.db.bootstrap` from the repo root
  (without ENVIRONMENT set to development or test it will not create the first administrator with a weak password). It creates the
  schema from the models and stamps the alembic head (the historical migration chain cannot be
  replayed from an empty database). On an existing database it simply runs `alembic upgrade heads`.
- `backend/tests/conftest.py`, alembic `env.py` and the bootstrap all read the same
  `POSTGRES_SERVER`/`POSTGRES_PORT`/`POSTGRES_USER`/`POSTGRES_PASSWORD` variables.

### Testing Requirements

**CRITICAL: Always Use Virtual Environment**
- **Before running ANY tests, ALWAYS activate the virtualenv first**
- The virtualenv is at `venv/` in the repository root
- Activation command: `source venv/bin/activate`
- **Never run tests without activating venv first - this will cause import errors**

```bash
# Correct workflow for running tests:
cd "$(git rev-parse --show-toplevel)"
source venv/bin/activate
export APP_ENV=test
export TESTING=true
python -m pytest backend/tests/ -v

# Run specific test files
source venv/bin/activate && python -m pytest backend/tests/unit/core/test_rules_engine.py -v

# Run with markers
source venv/bin/activate && pytest -m "unit" -v
```

**Test Best Practices**:
- Write comprehensive tests for all new features
- Use `pytest` with appropriate markers (unit, integration, api)
- **Every bug fix carries a `@pytest.mark.regression` test** that fails on the
  old code. The `regression-guard` job fails a pull request labelled `bug` that
  changes no test file; `.github/pull_request_template.md` asks for the test by
  name. Declare markers in `pyproject.toml` (`[tool.pytest.ini_options] markers`)
  and run them with `pytest -m regression`.
- Clear schema cache between tests with `clear_schema_cache()`
- **No test reaches real AWS.** The root `conftest.py` loads
  `backend/tests/no_real_aws.py` (the SDKs carry a copy): dummy credentials, no
  `~/.aws`, and a failing `aws` first on `PATH` that prints `NO-REAL-AWS`. A
  test that needs the CLI prepends its own fake `aws` to `PATH`; that one wins.
- Ensure PostgreSQL is running locally before tests
- Test users must have a `hashed_password` value set to avoid integrity errors
- **A test the claims registry names** (`docs/_claims/`, `scripts/doc_claims.py`): renaming, moving or deleting it fails Unit Tests (`backend/tests/unit/docs/test_doc_claims.py`) until the registry follows. `python scripts/doc_claims.py check` names each entry that points at it; change its `by` in the same pull request.

### Authentication & Permissions
- Feature flag and report dependencies are async (use `await`)
- Experiment dependencies are synchronous
- Use role-based access control (ADMIN, DEVELOPER, ANALYST, VIEWER)
- Permission checking: `check_permission(user, resource_type, action)`
- Mock authentication in tests with proper user objects including `hashed_password`

### Key Implementation Notes

#### Experiment Scheduling
- Experiments support automatic start/end date scheduling
- Background scheduler runs every 15 minutes checking for state transitions
- API endpoint: `PUT /api/v1/experiments/{experiment_id}/schedule`
- Must be in DRAFT or PAUSED status to schedule

#### Feature Flag Rollout Schedules
- Gradual rollout system with configurable stages
- API endpoints under `/api/v1/rollout-schedules`
- Background scheduler processes active rollouts
- Supports time-based, metric-based, and manual triggers

#### Safety Monitoring
- Automated safety checks for feature flags monitoring error rates, latency
- API endpoints under `/api/v1/safety` (`/settings`, `/feature-flags/{id}/config|check|rollback`)
- Rollback mechanism for problematic feature flags. A rollback to 0% turns the flag off
  (status INACTIVE, so every user gets `enabled: false`, `reason: "inactive"`, targeting-rule
  matches included); a rollback to 1-100% lowers only the global percentage, and users matched
  by a targeting rule keep their rule's percentage. Both pause every ACTIVE rollout schedule of
  the flag (`details["paused_schedules"]`), so a stage cannot raise it again (#629, #729;
  `SafetyService.execute_rollback`'s docstring is the contract)
- `SafetyService` (`backend/app/services/safety_service.py`) reads error metrics from `error_logs` and latency from `raw_metrics`; a missing per-flag config is returned as a default with `id = DEFAULT_CONFIG_ID` (nil UUID), never a 404

#### Public tracking API and conversion matching
- SDKs use `POST /api/v1/tracking/assign` (sticky assignment by `experiment_key`), `POST /api/v1/tracking/track`, `POST /api/v1/tracking/batch`, `GET /api/v1/feature-flags/evaluate/{key}?user_id=` with `X-API-Key`. The `/events` and `/assignments` routers are informational stubs.
- The tracking API stores the event's own name in `event_type` (e.g. `"purchase"`), while `EventService.track_conversion` stores `"conversion"`. **Every analysis path must match conversions with `backend/app/services/event_matching.py`** (`conversion_event_filter(metric.event_name)`), never `Event.event_type == "conversion"`.
- Experiments need a public `key` (auto-slugged on create) for the tracking API to find them.

#### Multi-Armed Bandits
- `BanditSchedulerRunner` starts in the app lifespan (`BANDIT_UPDATE_INTERVAL_MINUTES`, default 5; skipped when `APP_ENV=test`). Stats source order: DynamoDB `get_experiment_counters` → PostgreSQL (assignments + primary-metric conversions) → `BanditState` → zero priors. DynamoDB is used only when, for every variant, its pulls and successes are both at least PostgreSQL's (#430); otherwise PostgreSQL wins, so a partial DynamoDB count never replaces the complete one.
- `/tracking/assign` routes new users by `BanditState.variant_weights` for `optimization_type != "fixed"`; existing assignments are always kept.

#### Rate limits and CORS
- SDK paths (`/api/v1/tracking/*`, `/feature-flags/evaluate/*`, `/feature-flags/user/*`, `/api/v1/sdk/*`) share `SDK_RATE_LIMIT_PER_MINUTE` (default 6000/min per IP); auth endpoints stay strict; every `/api/v1/export/*` path shares ONE counter of 10/min per IP (`EXPORT_RATE_LIMIT`, keyed on the prefix by `rate_limit_key()`); everything else 300/min. Limits resolve via `resolve_rate_limit()` in `backend/app/middleware/rate_limiter.py`. If Redis fails, `RedisRateLimiter` counts per process (limits multiply by the number of API processes) and retries Redis every 30 s (`REDIS_RETRY_SECONDS`) with client retries off (`retry=Retry(NoBackoff(), 0)` -- redis-py's default ten retries would block the event loop for seconds on every retry); each switch logs once with a `rate_limiter` field.
- `CORS_ORIGINS` is a plain comma-separated list (annotated `NoDecode`); `BACKEND_CORS_ORIGINS` must be a JSON array. Dev defaults include ports 3000, 3001, 3100, 3200, 8000.

#### Demo applications
- `./demo/setup-local.sh` seeds demo data, starts the API (8000), dashboard (3100), the **ShopLab** storefront (`demo/shoplab`, 3200), **StreamPulse** (`demo/streampulse`, 3300) and their simulators. Pieces by hand: `python backend/scripts/seed_shoplab.py` / `seed_streampulse.py` (idempotent; `--reset`, `--no-history`; write `demo/<app>/.api_key` and `.env.local`), `cd demo/<app> && npm run dev`, `python demo/<app>/simulator/traffic.py --rate 3`, and for StreamPulse `python demo/streampulse/simulator/rollout_story.py --auto` (needs the backend running with `SAFETY_CHECK_INTERVAL_MINUTES=1 ROLLOUT_CHECK_INTERVAL_MINUTES=1`).
- Both demo apps consume the React SDK from source (`sdk/react/src`) via a webpack/tsconfig alias; tests there mock the SDK.

#### Feature-flag targeting and assignment eligibility
- Flag `targeting_rules` are stored in the **dashboard shape** (`{"logical_operator","groups":[{"logical_operator","conditions":[{"attribute","operator","value"}]}]}`, operators like `equals`, `semver_gte`, `in`). `backend/app/core/targeting_adapter.py` normalises that (and the native `TargetingRules` shape) to the Enhanced Rules Engine; the legacy list shape (`[{"type": "context", ...}]`) still takes the old path. `expand_context()` aliases attributes (`country` ↔ `user.country`, nested dicts flatten to dotted keys). Use these helpers anywhere rules are evaluated; do not add a fourth format.
- SDKs send user attributes as `context` (URL-encoded JSON) on `GET /feature-flags/evaluate/{key}`; responses carry `reason` (`targeting_rule|rollout|inactive|error`).
- `GET /api/v1/sdk/ruleset` (beta, #226) serves every flag normalised for server-side SDK-local evaluation, built by `backend/app/services/sdk_ruleset.py`; it requires an API key with the `sdk:ruleset` scope (`backend/app/api/sdk_scope.py`; `backend/tests/smoke/test_sdk_scoped_routes.py` pins which routes need it). `tests/sdk-contract/ruleset-vectors.json` is generated from the server (`python -m backend.scripts.generate_ruleset_vectors`); any change to the rules engine, the targeting adapter or the flag service's evaluation that moves an answer fails `test_sdk_ruleset_vectors.py` until the file is regenerated, and then the SDKs must agree with it.
- `AssignmentService.assign_user` checks global holdout → mutual exclusion group → experiment targeting for NEW users; ineligible users get HTTP 200 with the control variant and `assigned: false, reason: holdout|mutual_exclusion|targeting`, no assignment row, no exposure.
- Errors are reported with `POST /api/v1/tracking/errors[/batch]` (API key) → `error_logs`, stamped `request_data.source` `"server"` or `"client"` from the presented key (`report_source`). The safety monitor counts every row except `"client"` ones (`SafetyService.get_error_metrics`, `IS DISTINCT FROM`, so evaluation failures count), divides them by flag evaluations and rolls back to the config's `rollback_percentage`. Only an `sdk:ruleset` key whose owner can change flags stamps `"server"` (`presented_key_is_server_key`); any other key's reports are stored and shown as `uncounted_error_count`. A new `error_logs` writer must be listed in `backend/tests/smoke/test_error_log_writers.py`. Cadences: `SAFETY_CHECK_INTERVAL_MINUTES`, `ROLLOUT_CHECK_INTERVAL_MINUTES`, `BANDIT_UPDATE_INTERVAL_MINUTES`.
- `backend.app.models.register_core_models()` imports every *core* model module so standalone scripts can configure the SQLAlchemy mappers; add a new core model to `CORE_MODEL_MODULES` there. Module models are registered through `hooks.register_model_module()` by the modules registration (`modules/backend/app/register.py`), and schema builders load them with `modules_loader.require_modules_or_absent()`.

#### Metrics Import Standards
- Always use fully qualified imports: `from backend.app.models.metrics.metric import RawMetric, MetricType`
- Avoid re-exports in `__init__.py` files
- Verify with `grep -rnE '^\s*(from|import) app\.' backend/ modules/ --include='*.py'`; any
  hit is the defect. A relative `from .base import Base` inside
  `backend/app/models/` is NOT -- it resolves to the same module object.

## Monitoring & Deployment

### CloudWatch Integration
```bash
# Create log groups
aws logs create-log-group --log-group-name /experimentation-platform/api
aws logs create-log-group --log-group-name /experimentation-platform/services
aws logs create-log-group --log-group-name /experimentation-platform/errors

# Deploy monitoring dashboards
cd infrastructure/cloudwatch
./deploy-dashboards.sh
```

### Application URLs
- Backend API: http://localhost:8000
- Frontend (dashboard, `npm run dev` = `next dev -p 3100`): http://localhost:3100
- API Documentation: http://localhost:8000/api/v1/docs (ReDoc at `/api/v1/redoc`; `/docs` answers 404)
- Health Check: http://localhost:8000/health

## Common Development Tasks

### Running Single Tests
```bash
# ALWAYS activate venv first!
source venv/bin/activate

# Run specific test file
python -m pytest backend/tests/unit/services/test_experiment_scheduler.py -v

# Run tests with specific marker
pytest -m "unit" -v

# Run tests without coverage (faster)
python -m pytest backend/tests/ -p no:cov
```

### Database Operations
```bash
# Check migration history
python -m alembic -c backend/app/db/alembic.ini history

# The two heads of a full checkout: the core chain and the `modules` branch
python -m alembic -c backend/app/db/alembic.ini heads

# Mark current database state
alembic stamp heads
```

### Debugging Common Issues
- Database connections must use `localhost` on macOS
- Clear caches when tests fail unexpectedly: `clear_schema_cache()`
- Two heads is the normal state of a full checkout, not an error to fix: `upgrade heads`,
  and name the head a new revision extends. Do NOT `alembic merge` them (see below)
- Watch for duplicate SQLAlchemy relationship definitions
- Ensure proper async/await usage in permission functions
- **CRITICAL**: Always activate virtualenv before running tests or Python commands

## Detailed Development Guidelines

### Code Style Standards
- **Lint and format with ruff** (`make lint` / `make format`). Ruff replaced
  black, isort and flake8 in the P1 lint gate: one tool, one config block
  (`[tool.ruff]` in `pyproject.toml`), the same version in CI, pre-commit and
  the venv. `.flake8` is gone; do not reintroduce black or isort. The one
  addition is `tests/acceptance/docs/ruff.toml`, which extends that block to
  ban pytest's skip and xfail in the docs-journey runner (#939).
- Follow PEP 8 for Python code
- Use type annotations for all functions and methods
- Document public functions with comprehensive docstrings
- Use Pydantic v2 patterns consistently:
  - Replace `validator` with `field_validator`
  - Replace class-based `Config` with `model_config = ConfigDict(...)`
  - Use `model_validator` for complex validations
  - Import from correct paths: `from pydantic import field_validator, model_validator, ConfigDict`
  - Use `from pydantic_settings import BaseSettings, SettingsConfigDict` for settings

### Critical Database Migration Guidelines
- Migration files are located in `backend/app/db/migrations/versions/`
- When creating new migrations:
  - Use `alembic revision --autogenerate --head <head> -m "description"` to generate
    migration files, naming the head it extends: the core head id from `alembic heads`
    for a core change, `modules@head` for a module's (that one lands in
    `modules/backend/app/db/migrations/versions/`)
  - **Always** check the generated file for accuracy, especially for complex changes
  - **Verify** that `down_revision` points to the correct previous migration ID
  - **Always use revision IDs, not migration names**, in the `down_revision` field
  - **Expand, then contract.** A migration in release N must not drop or rename anything
    release N-1's code uses; do it in two releases (add and move over in one, drop in a
    later one). During a rolling upgrade N-1 keeps serving against N's schema, and an
    older image meeting a newer database refuses to start (`may_run_alembic`,
    `backend/tests/integration/database/test_newer_database_rollback.py`). Neither its
    message nor anything else may tell an operator to delete `alembic_version` rows (#238).
- Running migrations:
  - Use `python -m alembic -c backend/app/db/alembic.ini upgrade heads` to apply migrations
  - Use `python -m alembic -c backend/app/db/alembic.ini history` to view migration history
  - Set environment variables properly: `POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation`
- Migration troubleshooting:
  - **Two heads is the steady state of a full checkout, not a problem**: the core chain and
    the `modules` branch are separate on purpose, so that deleting `modules/` leaves a
    consistent core chain. Never run `alembic merge` on them. The merge file lands in the
    *core* versions directory with `down_revision = (<core head>, 'modules_0001_rbac')`, and
    every core checkout -- which has no `modules_0001_rbac` -- then fails to load the script
    directory at all. Use `upgrade heads` (plural) and `--head <head>` on a new revision.
  - `upgrade head` (singular) fails with "Multiple head revisions are present"; that is the
    reminder to type `heads`, not a reason to merge.
  - For database/migration mismatches, use `alembic stamp heads` to mark the current state
  - **Always verify migration chain integrity before deploying**

### Metrics Model Import Standards (Critical)
**Issue**: Inconsistent import paths for metrics models (especially `RawMetric`) cause Python to treat them as distinct classes, resulting in:
- Memory duplication
- SQLAlchemy "Class is not mapped" errors
- Type checking failures
- Unexpected behavior during runtime

**Solution & Prevention**:
1. **Standardized Import Paths**: Always use fully qualified imports: `from backend.app.models.metrics.metric import RawMetric, MetricType`
2. **Never root an import at `app`** -- `from app.models.metrics.metric import RawMetric`
   is a *different module object* from the `backend.app`-rooted one, which is
   the whole defect. A relative `from .base import Base` inside
   `backend/app/models/` resolves to `backend.app.models.base`, the same
   object, and is fine.
3. **Avoid Re-exports**: Don't re-export classes in `__init__.py` files; import directly from the defining module
4. **Verification**: `grep -rnE '^\s*(from|import) app\.' backend/ modules/ --include='*.py'`.
   No output means the convention holds, which is the state of `main`. There is
   no script and no dedicated test for this: `standardize_metrics_imports.py`
   and `backend/tests/unit/metrics/test_metrics_imports.py` were cited here for
   months and neither has ever existed in this repository.

### Permission System Architecture

#### Role-Based Access Control (RBAC)
The `core/permissions.py` module implements a comprehensive RBAC system with four roles:
- **ADMIN**: Full access to all resources and actions
- **DEVELOPER**: Can create and manage experiments and feature flags
- **ANALYST**: Can view all data but cannot create or modify resources
- **VIEWER**: Read-only access to approved resources

#### Feature Flag Permissions
- **Access to a flag is by role, not ownership** (founder decision D11): ADMIN and
  DEVELOPER read, create, change and delete any flag; ANALYST and VIEWER read any flag
  and change none, not even one they own. The creator is recorded as `owner_id` (a
  UUID) and shown, but it is not an access rule.
- **The one decision is `can_act_on_feature_flag(user, owner_id, action)`** in
  `backend/app/core/permissions.py`: the role matrix first, then `FEATURE_FLAG_SCOPE`
  (all "any flag" today; per-flag locks or approvals would go there). Every route that
  changes a flag calls it -- the per-flag routes, bulk-toggle, and every rollout
  schedule and stage change (against the schedule's flag).
  `backend/tests/smoke/test_flag_change_routes_guarded.py` pins the exact set of
  mutating routes; a new one fails until it is classified there.
- Create is gated by the routed, async `deps.can_create_feature_flag`. The other flag
  helpers in `deps.py` (`get_feature_flag_access`, `can_update_feature_flag`,
  `can_delete_feature_flag`) are **not used by any route**; do not build on them.
- Endpoints requiring authentication use `deps.get_current_active_user`; flag evaluation
  uses API key authentication via `deps.get_api_key`.
- In tests, "admin" fixtures are usually superusers, which bypass every check. Test
  permissions with a non-superuser of each role, and create flags through the API when
  the owner matters (fixtures that write `owner_id` directly hide create bugs).

### Async/Sync Pattern Guidelines
- **Feature flag and report-related dependencies are asynchronous** (use `await`)
- **Experiment-related dependencies are synchronous**
- When testing async functions:
  - Use `@pytest.mark.asyncio` decorator on test functions
  - Use `async def` for test function declarations
  - Use `await` when calling async functions
- **Critical**: Check for READ permission before UPDATE permission in the correct order
- Don't mix async and sync patterns incorrectly - follow the implementation in `deps.py`

### Test Environment Configuration

#### Database Setup Requirements
- Ensure PostgreSQL is running locally in Docker for tests
- Database connection string should use `localhost` on macOS
- Test users must have a `hashed_password` value set to avoid integrity errors

#### Complete Authentication Mocking for API Tests
For API tests that use tokens, mock multiple components of the auth system:

1. Mock the `get_current_user` and `get_current_active_user` dependencies
2. Mock token decoding to bypass AWS Cognito validation
3. Mock the Cognito auth service to return user data with appropriate groups

**Example comprehensive auth mocking**:
```python
@pytest.fixture
def admin_token(admin_user, monkeypatch):
    token = "mock_admin_token"

    # Mock get_current_user
    async def mock_get_current_user():
        return admin_user
    monkeypatch.setattr("backend.app.api.deps.get_current_user", mock_get_current_user)

    # Mock get_current_active_user
    def mock_get_current_active_user():
        return admin_user
    monkeypatch.setattr("backend.app.api.deps.get_current_active_user", mock_get_current_active_user)

    # Mock auth_service.get_user_with_groups
    def mock_get_user_with_groups(*args, **kwargs):
        return {
            "username": admin_user.username,
            "attributes": {"email": admin_user.email},
            "groups": ["admin-group"]  # This will map to ADMIN role
        }
    monkeypatch.setattr("backend.app.services.auth_service.CognitoAuthService.get_user_with_groups",
                       mock_get_user_with_groups)

    # Mock token decoder
    def mock_decode_token(*args, **kwargs):
        return {"sub": str(admin_user.id), "username": admin_user.username}
    monkeypatch.setattr("backend.app.core.security.decode_token", mock_decode_token)

    return token
```

#### Permission Testing Patterns
- Test both regular users and superusers when creating test cases
- Verify that proper 403 responses are returned when unauthorized actions are attempted
- Use mock objects that accurately represent actual objects including ownership properties
- All permission tests should validate that:
  - ADMIN users can perform all actions
  - DEVELOPER users can create and manage resources
  - ANALYST users can view but not modify resources
  - VIEWER users have read-only access
  - Unauthorized actions return 403 Forbidden responses

**For feature flag and report tests**:
- Use `@pytest.mark.asyncio` decorator
- Define test functions as `async def test_function_name`
- Use `await` when calling async dependency functions
- Properly mock both `check_permission` and `check_ownership` in permissions tests

**For experiment tests**:
- Use regular synchronous test functions
- Call dependency functions directly without `await`

### Safety Monitoring System

#### Database Schema
The safety monitoring system uses three main tables:
1. **safety_settings**: Global safety configuration
2. **feature_flag_safety_configs**: Per-feature flag safety configuration
3. **safety_rollback_records**: History of safety-triggered rollbacks

#### API Testing Patterns
```python
# For superuser endpoints
with patch("backend.app.api.deps.get_current_superuser", return_value=mock_user):
    response = client.get("/api/v1/safety/settings")

# For rollback endpoints, also mock the permission check
with patch("backend.app.core.permissions.check_permission", return_value=True):
    with patch("backend.app.api.deps.get_current_active_user", return_value=mock_user):
        response = client.post(f"/api/v1/safety/rollback/{feature_flag_id}")
```

## Feature Flag Rollout Schedules

The platform supports gradual rollout of feature flags through rollout schedules. This enables controlled, staged deployments of features with configurable criteria for progression.

### Key Components

1. **Rollout Schedules**: Define a plan for gradually increasing a feature flag's rollout percentage over time.
2. **Rollout Stages**: Individual steps within a schedule, each with a target percentage and conditions for activation.
3. **Triggers**: Criteria that determine when to progress to the next stage (time-based, metric-based, or manual).

### Database Models

- `RolloutSchedule`: Main model for rollout schedules.
- `RolloutStage`: Model for individual stages within a schedule.

### API Endpoints

All rollout schedule endpoints are available under `/api/v1/rollout-schedules`.

- `POST /`: Create a new rollout schedule
- `GET /`: List rollout schedules with optional filtering
- `GET /{schedule_id}`: Get a specific rollout schedule
- `PUT /{schedule_id}`: Update a rollout schedule
- `DELETE /{schedule_id}`: Delete a rollout schedule
- `POST /{schedule_id}/activate`: Activate a rollout schedule
- `POST /{schedule_id}/pause`: Pause an active rollout schedule
- `POST /{schedule_id}/cancel`: Cancel a rollout schedule
- `POST /{schedule_id}/stages`: Add a stage to a rollout schedule
- `PUT /stages/{stage_id}`: Update a rollout stage
- `DELETE /stages/{stage_id}`: Delete a rollout stage
- `POST /stages/{stage_id}/advance`: Manually advance a stage

### Scheduler

The `RolloutScheduler` runs in the background to automatically process active rollout schedules. It:

1. Checks for schedules with pending stages that are eligible for activation
2. Updates feature flag rollout percentages according to the stages
3. Transitions stages and schedules through their lifecycle (pending → in_progress → completed)

### Usage Example

```python
# Create a new rollout schedule
schedule_data = {
    "name": "Gradual Rollout for Feature X",
    "description": "Gradually roll out Feature X over 3 weeks",
    "feature_flag_id": "123e4567-e89b-12d3-a456-426614174000",
    "start_date": "2023-12-01T00:00:00Z",
    "end_date": "2023-12-31T23:59:59Z",
    "max_percentage": 100,
    "min_stage_duration": 24,  # Minimum 24 hours between stages
    "stages": [
        {
            "name": "Initial Rollout",
            "description": "First 10% of users",
            "stage_order": 1,
            "target_percentage": 10,
            "trigger_type": "time_based",
            "start_date": "2023-12-01T00:00:00Z"
        },
        {
            "name": "Expanded Rollout",
            "description": "Expand to 50% of users",
            "stage_order": 2,
            "target_percentage": 50,
            "trigger_type": "time_based",
            "start_date": "2023-12-15T00:00:00Z"
        },
        {
            "name": "Full Rollout",
            "description": "Roll out to all users",
            "stage_order": 3,
            "target_percentage": 100,
            "trigger_type": "manual"
        }
    ]
}
```

### Common Gotchas

1. Rollout percentages must be non-decreasing across stages (e.g., 10% → 50% → 100%).
2. Active schedules cannot have their stages deleted.
3. Stage orders must be sequential without gaps.
4. For time-based triggers, ensure the dates are in UTC timezone.
5. Manual stages must be explicitly advanced using the API.

## Enhanced Rules Engine (EP-001)

The platform includes a comprehensive enhanced rules evaluation engine with advanced operators and performance optimizations.

### Key Features

1. **Advanced Operators**: 20+ operators including semantic version comparison, geo-distance, time windows, JSON path, array operations
2. **Performance Optimizations**: Rule compilation caching, evaluation result caching, batch evaluation support
3. **Metrics Collection**: P50, P95, P99 latency tracking with configurable sample windows
4. **Thread Safety**: Concurrent evaluation support with proper locking mechanisms

### Core Components

- **RulesEvaluationService**: High-level service integrating caching, compilation, and metrics (`backend/app/services/rules_evaluation_service.py`)
- **RuleCompiler**: Rule compilation and validation with LRU caching (`backend/app/core/rule_compiler.py`)
- **EvaluationCache**: Thread-safe LRU cache for evaluation results with TTL support (`backend/app/core/evaluation_cache.py`)
- **Rules Engine**: Core evaluation logic with all operator implementations (`backend/app/core/rules_engine.py`)

### Operator Reference

See `/docs/Enhanced_Rules_Engine_Reference.md` for complete operator documentation including:
- Usage examples for all 20+ operators
- Performance characteristics
- Best practices and anti-patterns
- Real-world scenario examples

### Test Coverage

- **131+ tests** covering all aspects of the enhanced rules engine
- Days 1-2: Advanced operator tests (41 tests)
- Days 3-4: Compilation and caching tests (54 tests)
- Days 5-6: Service layer tests (21 tests)
- Days 7-8: Integration tests (15 tests)
- All tests passing with 51% overall code coverage

### Performance Targets

- Simple operator evaluation: > 100k ops/second
- Complex operators (semver, geo): > 1k ops/second
- 1000 user batch evaluation: < 5 seconds
- Cache lookup: < 100ms for 10k lookups
- Rule compilation cache provides 1.3-1.7x speedup

## Git Workflow for Claude Code

**The pull request title is the release note.** `main` is squash-merged and
GitHub makes the squash subject the pull request title, which is what
release-please parses to build the changelog and choose the version bump. A
title that is not a conventional commit is invisible to it -- the release is
still cut, the work simply does not appear in it and does not move the
version. That is what happened to P3.5 (#165/#167): its subject was prose, and
the release pull request it produced contained three historical `docs:`
commits and nothing from the change that had just landed.

So every pull request title is `type(optional scope): description`, with
`type` one of `feat fix perf deps docs build ci refactor test chore revert
style` -- the set `release-please-config.json` understands. The
`conventional-title` job checks it and re-runs when the title is edited. If
you pass `--subject` to `gh pr merge`, that wins over the title, and it has to
satisfy the same rule.

When committing changes:
1. Use `git status` to check changes
2. Use `git diff` to review modifications
3. Stage files with `git add <files>`
4. Create detailed commit messages following the format:
   ```
   Brief summary (50 chars or less)

   Detailed explanation of changes:
   - What was changed
   - Why it was changed
   - Any technical details

   🤖 Generated with [Claude Code](https://claude.com/claude-code)

   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
5. Push with `git push origin main`

## How work is done here: plan, review, sign-off, then build

**Founder instruction, standing, for every session.** For anything beyond a
single-file change:

1. **Draft the plan with the three drafting roles** (`.claude/agents/`), in
   parallel: `software-architect` writes DESIGN and SPEC, `qa-engineer` writes
   VERIFICATION and NOT VERIFIED, `ux-designer` writes the user- and
   reader-facing side (dashboard, demo apps, error messages, SDK surface,
   documentation). You merge their drafts into one document with the shape
   below, and say where they disagreed and what you chose.
2. **`principal-engineer` pressure-tests the merged plan**: every factual
   premise checked by running something.
3. **`engineering-manager` gives the final verdict**, with the principal
   engineer's findings in front of it: scope, size, sequence, what is
   irreversible or blocked on a human, and whether the principal engineer's
   conditions are met or accepted. A rejection or a condition is not advice;
   address it and re-submit. An APPROVED verdict, or APPROVED WITH CONDITIONS
   whose conditions you have met, is the gate.
4. **Only then write code.**

All five review the PLAN, not code. (Founder instruction, 2026-09-25: the
drafting roles were added after two rounds in which the author wrote the plan
alone and the reviewers each found a premise the author had not checked.)

The reviewers' job is to **pressure-test and verify the assumptions**, not to
nod. `principal-engineer` checks every factual premise by running something,
because the expensive failures here have all started from a premise that looked
reasonable and was false -- "the workflow already installs the lock" (it
resolves without installing), "the deploy runs cdk deploy" (both hits are
comments, one saying the opposite), "the root package.json is unused" (a test
opens it at a path built at runtime). `engineering-manager` owns scope,
sequencing, size and what is irreversible or blocked on a human.

**Why this exists.** Three consecutive review rounds on PR #236, each finding a
defect in the previous round's *fix*, twice nearly shipping a silent outage.
The plan-first instruction dates from 2026-09-22; the sign-off requirement was
added after a session in which it was not followed and the same traps were
walked into again -- a mandatory setting that would have failed the first
deploy, and a wildcard pattern that matches nothing and refuses all traffic
while the health checks stay green.

### How to actually consult the team

**Three rounds, in order.** Draft, in one message, three calls:

    Agent(subagent_type="software-architect", prompt=...)
    Agent(subagent_type="qa-engineer",        prompt=...)
    Agent(subagent_type="ux-designer",        prompt=...)

merge their drafts into the plan, then pressure-test it:

    Agent(subagent_type="principal-engineer", prompt=...)

then the final verdict, with the principal engineer's review passed by path:

    Agent(subagent_type="engineering-manager", prompt=...)

Persist every draft to `drafts/v<n>-<role>.md` and every verdict to
`reviews/v<n>-<role>.md`. A drafter with nothing to say for this change (no
user-facing effect, say) says so in one line; do not invent scope to fill a
section.

They are defined in `.claude/agents/`. **If those names are not in your
available-agents list, they were added after your session started** — the list
is fixed at session start. Fall back to `general-purpose` and open the prompt
with "You are the ENGINEERING MANAGER. Read your brief first:
`.claude/agents/engineering-manager.md`". That is exactly how the first two
rounds of the launch-readiness plan were run.

**Every reviewer is a fresh invocation with no memory of its previous round.**
It cannot recall the conditions it set last time. So:

- **Persist every verdict to disk the moment it arrives** — the plan's own
  directory, `reviews/v<n>-<role>.md`, with the numbered conditions verbatim.
  The first round of this was lost to a scratchpad and the manager had to ask
  for its own conditions back.
- **Pass prior reviews by path, not by paste.** A path cannot be paraphrased
  into something easier to satisfy.

**What each prompt must carry:**

1. The path to the plan, and to any previous version.
2. The path to `DECISIONS.md`, with the settled items named — *"these are
   settled, do not re-litigate them, but DO judge whether the revision
   discharges them."* Reviewers will otherwise reopen founder decisions, which
   wastes a round.
3. Any facts discovered since the last round, so they are checked rather than
   assumed.
4. **The specific things you most want pressure-tested** — including the
   weaknesses you already suspect. Naming your own weak point gets it examined
   rather than missed; the Stream A red-main hazard was found because it was
   asked about directly.
5. Read-only constraints, explicitly: no writes, no AWS calls that create, no
   publishing.

**Expect rejection, and do not treat it as failure.** Across two rounds the
reviewers caught a required check that was green for the wrong reason, a
documentation tag that destroys the fence it annotates, a deploy that reports
healthy while rejecting every request, a migration that can run production's
image against staging, and a release gate no tag can satisfy. Each of those
was cheaper to find in review than in production.

**Then meet the conditions rather than noting them**, and apply the cap: two
rejected revisions and you stop, work the uncontested parts, and write down
what is contested and why.

### A blocking question goes to the team, not to a halt

**Founder instruction, standing.** When work is blocked on a question the
founder has not answered — and especially when nobody is available to answer —
**put it to `engineering-manager` and `principal-engineer`, and go with their
recommendation.** Do not stall, and do not guess silently.

Three rules keep this honest:

- **Record it.** The question, the recommendation and the reasoning go in
  `DECISIONS.md` alongside the founder's own decisions, marked as team-decided
  rather than founder-decided, so the distinction survives.
- **It does not extend to the irreversible.** A team recommendation cannot
  authorise publishing a package, creating infrastructure that bills, a force
  push, an announcement, or anything else on the IRREVERSIBLE list. Those wait
  for a human however long that takes.
- **Cap the rounds.** If reviewers reject a revision twice, stop revising.
  Work the uncontested parts and leave a written note. Iterating against
  reviewers with nobody to break a tie is how the fix-round spiral starts, and
  that is the failure this whole process exists to prevent.

### Use a SPEC where it earns its place

For a change with a **contract** -- a new setting, a new gate, a new endpoint,
a new workflow, a format other things parse -- write the spec before the code
and put it in the plan:

* **What** it does, in one paragraph.
* **The contract**: inputs, outputs, defaults, and what is refused. Name the
  values, not the shapes: `ALLOWED_HOSTS` accepts `example.com` and
  `*.example.com`; it refuses `*example.com`, which matches nothing.
* **Where it runs** and what must already be true there.
* **Failure modes**, including the silent ones. What does it look like when
  this is misconfigured rather than broken?
* **Verification**: for each property, the defect that proves the check works.

A spec is not ceremony for a one-line fix. It is how a change whose behaviour
other things depend on gets its edges decided before they are discovered.

### The plan's shape

    GOAL          what this closes, and how you will know it is closed
    PREMISES      every factual claim, each with what you ran to check it
    DESIGN        what changes, and what deliberately does not
    SPLIT         why this is one change and not several
    SEQUENCE      what must be true before each step
    ENVIRONMENTS  where it runs vs where you can verify it
    IRREVERSIBLE  what needs a human at the moment it happens
    VERIFICATION  per property: the gate, and the defect that proves it fires
    NOT VERIFIED  what you could not check, and why

The last line is mandatory and it is not an admission of weakness. "This venv
has no `constructs._jsii`, so CI is the first thing that runs the synth suite"
is the sentence that tells a reviewer where to look.

## Verifying a change (read this before saying something works)

Three consecutive review rounds on one pull request each shipped a regression
created by the previous round's fix. Every one of them was invisible locally
and obvious in CI, for the same reason: **the developer environment is the one
place those bugs cannot appear.** The venv has every package installed, the
tree has both profiles present, the database is already migrated. CI, the
container and production have none of those properties.

So: a green local suite is the weakest evidence available. It says the code
works in the environment least like the one it runs in.

### Verify in the environment the code actually runs in

- Before claiming a dependency change is safe, install **exactly** what the
  target installs. `backend/requirements.txt` is what every test job and
  developer venv gets; `backend/requirements/runtime.lock` is what the image
  gets; `modules/requirements.txt` is what the full image adds. A pin that
  moves between them changes which environments can import it.
- Before claiming a profile works, build the tree that profile actually ships:
  `scripts/core_build.sh` (a copy with `modules/` deleted), not the checkout
  with an environment variable set. Nothing under `backend/` reads
  `EXPERIMENTLY_PROFILE`; the profile is decided by whether `import modules`
  succeeds.
- Before claiming a migration works, rehearse the **transitions**, not the
  states: a core database met by a full image, a full database met by a core
  image, and a database at the previous release's revision upgraded with
  `heads`. Print the `alembic_version` rows and the affected constraints after
  each. `db/bootstrap.py` repairs things the raw `alembic upgrade heads` that
  production runs does not.

### Pin the mechanisms that act at a distance

Alembic's revision graph, alembic's reflection scope and the dependency
closure all have non-local effects: a correct-looking local edit changes
behaviour somewhere no local test looks. Those properties are asserted
literally — the exact head set, the exact apply order, an autogenerate that
produces an **empty** migration against a freshly bootstrapped database — so a
silent reorder or a widened reflection fails loudly with a diff instead of
quietly proposing `op.drop_table` against a production schema. Do not relax
those tests to make a change pass; they are the contract.

### When a rehearsal repeats, make it a test

If you find yourself running the same manual rehearsal in successive rounds of
work, it belongs in CI. That is where the transition tests in
`backend/tests/integration/database/` came from.

### Reviewing your own fixes

After a round of fixes, aim the next review at the **diff of the diffs** —
what the fixes changed — rather than at the whole change again. That is where
the remaining risk is. And distinguish the two kinds of finding: a bug that
became *reachable* because a gate was removed is pre-existing and finite;
a bug the last fix *created* is the signal that the mechanism needs a pin.

### A tool does not do what its flag is called

Almost every wrong thing written into the P3.5/P3.6 gates came from believing a
name instead of running the thing. In one pass, all of these were false:

- `pip-audit --no-deps` does **not** stop pip resolving. 75 pins in
  `backend/requirements.txt` audited **179** packages, including one the file
  does not pin. `--disable-pip` is what stops it, and pip-audit rejects that
  flag on an unhashed file unless `--no-deps` is *also* given.
- `--strict` did nothing, because the exit status was discarded. A package
  pip-audit cannot resolve comes back as `skip_reason` with no `vulns`, so the
  gate printed "no advisories" over a package it never audited.
- `rm -rf` on a path that does not exist **exits 0**. Every cleanup line in a
  Dockerfile fails open unless something afterwards asserts the result.
- `MANIFEST.in`'s `include VERSION` is not what puts `VERSION` in the sdist —
  setuptools adds a dynamic-version file itself, and `exclude VERSION` does not
  even take it out.
- `license-checker` on a tree with no `node_modules` **exits 0** reporting one
  entry, the package itself. It does not error.
- `gitleaks` deliberately ignores AWS's published example key, so probing with
  `AKIAIOSFODNN7EXAMPLE` makes a working scanner look broken.
- `toJSON(needs)` is `{job: {result, outputs}}`, not `job=result` pairs.
- `npm audit --omit=dev` still reports a package that is a peer dependency
  *and* a devDependency.

So: before a flag, a default or an absence-of-error becomes load-bearing, run
it once and look. One command is cheaper than the review round that finds it.

### Tamper-test the gate where the gate runs, not where you are

"Verify in the environment the code runs in" applies to the *gate and its
tests*, and this is where it keeps biting. Three times in one PR:

- a gate test made its fixture with `git worktree`, which fails in
  `core_build.sh`'s deliberately non-repository copy;
- a licence gate covering six npm closures was **vacuous in CI**, because the
  job installs only `frontend` — it "found" something locally purely because
  this laptop had the other five installed;
- the test written to fix that took the same shortcut and passed locally,
  failed in CI.

Cheap moves that would have caught all three: `mv frontend/node_modules` aside
and re-run; `git archive HEAD` into a plain directory and run there; build the
image and inspect it rather than reasoning about the `COPY`.

**And when a tamper does not fire, suspect the probe before the gate.** A
gate that looks broken is usually a bad probe — the example-key case above cost
a round-trip of believing gitleaks was rule-less again.

### The comment saying *why* is the likeliest false line in the diff

Code is checked by tests; the justification beside it is checked by nothing, and
it is written from intent before the thing is measured. In one PR: a
`MANIFEST.in` comment that named the wrong mechanism, a CodeQL header
describing an upload that had become conditional, "neither branch below would
accept it" about a mixed SPDX expression that was accepted, and "all three are
clean" about three files that are *empty* — not the same claim. Write the
comment after measuring, or write it as intent.

### Absence of a reference is not absence of a dependency

Before deleting a file, four negative checks said the repository root
`package.json` was unused: no `git grep` hit, no lockfile beside it, no
`npm ci` at the root in any workflow, no `/` entry in `.github/dependabot.yml`.
Every one of them was true. CI failed anyway —
`frontend/src/tests/modules-alias.test.ts` opens it at a path built from
`REPO_ROOT`, asserting the repo root declares no module `"type"`, because
Turbopack takes the module format from the nearest package.json above
`modules/frontend/src`.

No search for the string `package.json` could have found that. The consumer
builds the path at runtime, and what it asserts is the file's *contents*.

So a deletion is justified by **running the suites that could plausibly touch
the thing**, not by a grep that comes back empty. Ask what would read this
without naming it: a path composed from a root constant, a directory listing, a
glob, a framework resolving "the nearest X above Y", a fixture discovering
files. And note which suites you could not run locally — the frontend suite is
the one that caught this, and `make lint` never reaches it in a checkout with
no `frontend/node_modules`.

### Every gate must be tamper-tested

The most frequent defect in this repository is not broken code — it is a check
that passes for the wrong reason. Found in one review cycle: `find -name
'workspaces*'` matching the chunk *directory* instead of the page it guarded;
a "core image ships no module packages" assertion made of six hand-typed names
that already missed two; a `profile: core` CI input that selected nothing,
so the check named `(core)` tested the full profile; a `make` recipe guard
whose `exit 0` ended only its own line's shell; `--steps <typo>` skipping every
step and exiting `OK`; a CI gate that called `load_modules(force=True)` and
thereby erased the failure it existed to detect; a route allow-list keyed on
prefixes, so a new anonymous route under an existing prefix passed; and a CDK
test that stubbed `App.synth` to a no-op, so the app was never synthesised.

So: **when you add or change a gate, break the thing it guards and watch it
fail.** Paste that evidence in the report. A gate you have only ever seen pass
is not a gate. This applies to assertions, CI steps, shell guards, lint scopes
and tests alike.

That includes a check you *generate* rather than type. A grep written into four
instruction files through a Python f-string arrived double-escaped (`\\s` for
`\s`) and matched nothing — it would have reported "clean" for ever. Every
layer of quoting between you and the thing that runs is a place for the check
to die silently, so run the emitted command against a planted offender and
watch it fire.

### A new gate's first failures are evidence, not defects

The first run of the export sweep failed two of its five checks: ten forbidden
claims and 197 dangling references. None of the 197 was a dead link -- 205 of
them were the *word* "project", matched by a directory entry with no trailing
slash, and ten were `.env.dev`, a file the docs legitimately tell you to
create. Eight of the ten claim hits were absolute paths in the `.claude/agents`
and `.claude/commands` files that had just been *carved out* of the exclusion
list to be kept; the other two were the script matching its own pattern.

Two rules from that:

- **Read the evidence file before fixing anything.** A text-matching gate over
  a codebase must anchor on path-shaped references (a removed directory is
  `project/`, not `project`; a removed file is its full path), must exclude its
  own source, and must distinguish "committed by mistake" (`.env.dev`) from
  "documentation people link to". Fix the gate's precision first, then look at
  what is left -- and paste the *residual* hits in the report, not the raw count.
- **An exception you carve into an exclusion list is a new obligation.** The
  moment `.claude/agents` was kept, its files had to meet the same bar as the
  rest of the tree, and they did not. Re-run every sweep over whatever you just
  decided to keep.

### Prefer the dumbest thing that works

Twice in this repository a "safer" rewrite was less safe than what it replaced.
The readiness sanitiser replaced a plain 200-character truncation with three
regexes: it left credentials in place in the JSON and quoted forms its own
docstring gave as examples, and it backtracked quadratically — 12.9 s on a
5.5 kB line, minutes on a longer one, in a synchronous call inside an async
handler on a one-worker container, i.e. an unauthenticated denial of service.

In redaction, authentication, and anything parsing untrusted text: bound every
quantifier, truncate before processing rather than after, compare on bytes
(Starlette decodes headers as latin-1 and `hmac.compare_digest` raises
`TypeError` on a non-ASCII `str` — this has been a live 500 twice), and prefer
a boring construct you can reason about over a clever one you cannot.

### Do not hoist a repair into a shared path without enumerating its callers

Three defects in one cycle came from moving logic out of `db/bootstrap.py` into
`migrations/env.py` "so every path gets it". `reconcile_with_models` then ran
`create_all` on *every* `alembic upgrade` that reached head — creating
unmigrated tables in production, outside the migration transaction, invisible
to the next autogenerate. `may_run_alembic`'s upgrade-shaped test made
`downgrade` and `stamp` silent no-ops. And the third repair was left behind,
so the raw `alembic upgrade heads` that production actually runs still died.

Widening where something runs widens what it can break. List the callers, say
what each now gets, and scope the change to the ones that asked for it.

### A claim about behaviour needs a test, or say it is intent

The README promised "a route whose module is not installed answers 501" when
only three brokered routes did and the rest 404'd; the migration docs gave a
rollback command that had come to mean "drop all 26 revisions"; a Dockerfile
comment asserted that a glob matching nothing is tolerated (it is not, on the
classic builder); a code comment said three webhooks authenticated their
sender when two verified nothing at all. Operators follow prose. If a
statement is about observable behaviour — a status code, a default, a command
— either pin it with a test or write it as intent, not as fact.

### Running commands

Never pipe a command whose failure matters through `tail` or `head`: it
replaces the exit status with the pipe's, and a killed run then reads as
success. Three agents and I were each fooled by this in one session. Redirect
to a file and read the file.

A comment inside a `\`-continued command ends it. Explaining an `aws ecs
run-task` flag on its own line mid-continuation left the command running with
four arguments and the rest parsed as separate commands; actionlint's SC2215
caught it, and that code is not among the five the lint job suppresses. Put the
comment above the command.

For a bulk rewrite across many sites, check every *shape* of site afterwards,
not the first one — a scripted edit here silently mangled the blocks that
compared two timings while getting the single-timing ones right.

Never assert a single wall-clock timing. Three separate flaky gates in this
repository came from `assert duration < X` or `assert duration2 < duration1`;
take the best of several runs (what `timeit` does), or assert on the
deterministic thing the timing was standing in for.

### The merge bar

Merge when a review round produces no finding that would break a deployment or
lose data. Everything else becomes a tracked issue. Without a bar, review
rounds never end, because each one finds something.

## Releasing, and the docs site

Development happens in this repository now. There is no private repository and
no export step: a push here is publication, and `scripts/leak_guard.py` is what
stands between a secret and the world (see `.github/workflows/leak-guard.yml`).
`scripts/publish/export.sh` and its exclusion list are gone; anything that
genuinely cannot be public lives in `getexperimently/experimently-internal`.

What remains non-obvious, every item of which cost a wrong report or a failed
deploy:

### A `GITHUB_TOKEN` event triggers no workflow

GitHub refuses to run workflows for events its own token created, to stop
recursion. Two consequences, both certain to recur:

- **The release-please pull request has ZERO checks.** With 20 required, it can
  never satisfy branch protection. It is not blocked on anything you can fix in
  the pull request; merge it with `--admin` after confirming the diff is only
  the eight files it owns: CHANGELOG.md, VERSION, `.release-please-manifest.json`
  and the five `extra-files` (see the next section).
- **The tag it pushes triggers nothing -- but `release.yml` does not need it
  to.** `release-please.yml` calls `release.yml` itself (`uses:
  ./.github/workflows/release.yml`, a `workflow_call`) when `release_created`
  is true, so merging the release pull request builds, signs and publishes the
  release. **Do not dispatch `release.yml` after a release.** A second run
  builds the release again and moves its image tags to the new digests; that
  happened to 0.17.0, two runs, and the first signed build was orphaned.
  `gh workflow run release.yml -f tag=vX.Y.Z` is only for re-running a release
  whose own `release.yml` run failed after the tag existed.
- **The docs site does need a dispatch.** `docs.yml` deploys only on a `v*`
  ref, and the bot's tag triggers nothing, so after each release run
  `gh workflow run docs.yml --ref vX.Y.Z`. A pre-release tag (anything with a
  `-`, e.g. `v1.2.0-rc.1`) does not deploy (#776).
- **Then walk the docs.** Once that deploy has finished, run
  `gh workflow run docs-journeys.yml --ref vX.Y.Z`: the docs journeys (#939)
  on the release's own commit, including the crawl of the site just
  published, with their artifacts kept 90 days. `release-please.yml` does not
  start it (a dispatch, like the docs deploy). A run that is red right after
  a deploy is dispatched once more before anyone reads it as a docs defect:
  Pages has answered 503 for a single page shortly after a deploy (one of
  121, about 35 minutes after v0.25.1), and such a page looks the same as a
  broken one. Two reds in a row are the finding.

A tag pushed by a human token DOES trigger workflows, which is why a
hand-pushed tag behaves differently: `release.yml` runs from the push.

Release-please also needs the ORGANISATION to allow it. If it fails with

    release-please failed: GitHub Actions is not permitted to create or
    approve pull requests

that is not a repository setting you can fix from here -- the repository API
answers `409 The organization does not allow GitHub Actions to create or
approve pull requests`. It is
`github.com/organizations/Controlvar/settings/actions` -> Workflow
permissions, and it needs an org owner.

### A release bumps VERSION and leaves the fixtures behind

Five committed files embed the version and are `extra-files` in
`release-please-config.json`: both OpenAPI snapshots under `docs/api/`
(`openapi-v1.full.json`, `openapi-v1.stable.json`), the frontend's copy
(`frontend/src/tests/fixtures/openapi.json`), `charts/experimently/Chart.yaml`
and `deploy/compose/compose.yml`. With CHANGELOG.md, VERSION and
`.release-please-manifest.json` that is eight files in every release pull
request (0.18.0, #749, is the example). If a release ever lands with the
OpenAPI copies stale, `make openapi` and commit; the smoke test
`test_version_sources.py` fails loudly either way.

Check a release pull request by diffing it: if anything but the version string
changed in those five files, the generator and the committed copies have
diverged; for the three OpenAPI files `make openapi` is the fix, not the
release.

### The docs site deploys from a TAG, and two settings gate it

`docs.yml` builds on every push to main but only deploys when the ref is a
`v*` tag. Both of these were wrong at once and the site 404'd for weeks while
the build stayed green -- the build passing tells you nothing about the deploy:

- **Pages must be enabled** (`build_type: workflow`). Otherwise the deploy step
  fails with `HttpError: Not Found ... Ensure GitHub Pages has been enabled`.
- **The `github-pages` environment must allow the tag.** Its deployment branch
  policy listed only `main`, so every tag deploy was rejected about two seconds
  in. It needs a `v*` entry of type `tag` alongside it.

When a deploy fails in seconds rather than failing to build, suspect the
environment policy before the artifact. And do not read a failed job's log
through the API to diagnose it: that call can itself return
`<Error><Code>BlobNotFound</Code>`, which looks exactly like a deploy error and
only means the log has gone. `/deployments/<id>/statuses` showed `waiting` then
`failure` two seconds apart and pointed straight at the policy.

### Verify where the thing runs, not where you are

The old version of this section said "verify in the exported tree, not this
one", because `mkdocs build --strict` passed locally and failed on the export
where `docs/planning/` had been stripped. There is no export now, but the rule
it came from is unchanged and still the most expensive one here:
`scripts/core_build.sh` copies the tree into a directory with **no `.git`**, and
a test that shells out to git passes everywhere except there. That is what
broke `core-build` on the pull request that introduced the leak guard.

## Dependabot: the queue is a merge problem, not a volume problem

The open-PR count reached 12-30 and was bulk-closed by hand at least three
times. Each time the response was to reduce the *rate* -- major ignores,
staggered schedules, lower `open-pull-requests-limit` (#280 did all three).
The queue came back within a day, every time.

The cause was never volume. **Nothing merged them.** There was no auto-merge
workflow and `allow_auto_merge` was `false` on the repository, so every bump
was merged by a human who happened to look. With 19 ecosystems producing
updates weekly or monthly, the queue grows monotonically between clean-ups by
construction. Closing them by hand resets the counter and changes nothing.

`.github/workflows/dependabot-automerge.yml` closes the loop:

- **patch and minor, any ecosystem** -- merges itself once the gate is green
- **github-actions, including majors** -- also merges itself, which is why
  `dependabot.yml` carries no major ignore for that one ecosystem: an Actions
  major is how GitHub forces you off a retired runner runtime, and CI passing
  *is* the test for one
- **majors everywhere else** -- left alone; they are migrations, and
  `dependabot.yml` ignores them anyway

`gh pr merge --auto` bypasses nothing. Branch protection still requires all 20
checks; a failing bump stays open and red, which is the signal you want.

So before reaching for the limits again: **if the queue is growing, ask what
is supposed to drain it.** A rate limit on an undrained queue only changes how
fast it fills. The same reasoning applies to any bot-generated backlog.

## Claude Code Best Practices

1. **Always activate virtualenv first** before running any Python commands
2. Read relevant documentation files in `/docs` before making changes
3. Check existing test patterns before writing new tests
4. Use the Task tool for complex multi-file searches rather than running grep directly
5. Run tests after making changes to verify functionality
6. Follow the established code style and import patterns
7. When fixing bugs, understand the root cause before implementing fixes
8. Reference file paths with line numbers when discussing code locations

## Common Patterns for Claude Code

### Running Tests
```bash
# Standard test workflow
source venv/bin/activate
export APP_ENV=test TESTING=true
python -m pytest backend/tests/unit/core/ -v --tb=short

# Quick test without coverage
source venv/bin/activate && python -m pytest backend/tests/ -p no:cov -q
```

### Database Migrations
```bash
# Create migration. `--head` names the head it extends: the core head id that
# `alembic heads` prints, or `modules@head` for a module's.
alembic revision --autogenerate --head <head> -m "description"

# Apply migration (`heads`, plural: a full checkout has two)
python -m alembic -c backend/app/db/alembic.ini upgrade heads

# Check migration status
python -m alembic -c backend/app/db/alembic.ini current
```

### Code Quality Checks
```bash
# Format and auto-fix backend/, modules/, scripts/ and tests/acceptance/ in
# place (ruff: format + lint + import order)
make format

# The full lint gate, in this order:
#   ruff check / ruff format --check   backend/ modules/ scripts/ tests/acceptance/
#   lint-imports                       the core/modules import boundary
#                                      ([tool.importlinter] in pyproject.toml,
#                                      backend/lambda/.importlinter)
#   reuse lint                         every file carries a licence, and
#                                      LICENSES/Apache-2.0.txt matches LICENSE
#   check_requirements_lock.py         both requirements locks match their inputs,
#                                      and the requirements files share their pins
#   check_legacy_name.py               the former name only where
#                                      scripts/legacy_name_keep.toml keeps it
#   eslint + tsc                       frontend/
#   hadolint                           the four Dockerfiles
#   actionlint                         .github/workflows
make lint

# Type checking. NOT part of `make lint` or any gate (no workflow, Makefile
# target or pre-commit hook runs it) and NOT clean: ~931 errors on main. A
# baseline gate or dropping it is #788; until then a mypy run is advisory.
mypy backend/app/
```
