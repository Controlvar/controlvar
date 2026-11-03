# Backend Development Guide

Backend-specific guidance for the experimentation platform FastAPI application.

## Quick Start

```bash
# Activate virtual environment (ALWAYS DO THIS FIRST!)
source venv/bin/activate   # from the repository root

# Run development server. Everything runs from the REPOSITORY ROOT: the code
# imports `backend.app.*`, so `cd backend && uvicorn app.main:app` puts the
# wrong directory on sys.path and the imports fail.
ENVIRONMENT=development uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
export APP_ENV=test TESTING=true
python -m pytest backend/tests/ -v

# Database migrations (run from the repository root; `heads` because a full
# checkout has two -- the core chain and the `modules` branch)
export POSTGRES_DB=experimentation POSTGRES_SCHEMA=experimentation
python -m alembic -c backend/app/db/alembic.ini upgrade heads
```

## Directory Structure

```
backend/
├── app/
│   ├── api/           # API endpoints (REST)
│   │   ├── deps.py    # Dependencies (auth, permissions)
│   │   └── v1/        # API version 1 routes
│   ├── core/          # Core functionality
│   │   ├── config.py           # Configuration
│   │   ├── permissions.py      # RBAC system
│   │   ├── rules_engine.py     # Rules evaluation
│   │   ├── rule_compiler.py    # Rule compilation & caching
│   │   └── evaluation_cache.py # Evaluation result caching
│   ├── db/            # Database
│   │   ├── migrations/         # Alembic migrations
│   │   └── session.py          # DB session management
│   ├── models/        # SQLAlchemy models
│   │   ├── experiment.py
│   │   ├── feature_flag.py
│   │   ├── user.py
│   │   └── metrics/            # Metrics models (use full imports!)
│   ├── schemas/       # Pydantic schemas
│   ├── services/      # Business logic layer
│   └── main.py        # FastAPI application entry
└── tests/
    ├── unit/          # Unit tests
    └── integration/   # Integration tests
```

## Import Patterns (CRITICAL)

### Always Use Full Qualified Paths

```python
# ✅ CORRECT - Full qualified imports
from backend.app.models.metrics.metric import RawMetric, MetricType
from backend.app.services.experiment_service import ExperimentService
from backend.app.core.permissions import check_permission, UserRole

# ❌ WRONG - Relative imports
from app.models.metrics.metric import RawMetric
from ..services.experiment_service import ExperimentService
```

### Why This Matters

Inconsistent imports cause Python to treat the same class as distinct objects, leading to:
- SQLAlchemy "Class is not mapped" errors
- Memory duplication
- Type checking failures

**Verification**: `grep -rnE '^\s*(from|import) app\.' backend/ modules/ --include='*.py'` --
any hit is the defect. A relative `from .base import Base` inside
`backend/app/models/` is not: it resolves to the same module object as the
fully-qualified path.

## Async/Sync Patterns

### Feature Flag Access

Access to a flag is by role, not ownership. Every route that changes a flag calls the
one rule, which is synchronous:

```python
from backend.app.core.permissions import Action, can_act_on_feature_flag


@router.put("/feature-flags/{flag_id}")
async def update_feature_flag(
    flag_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if flag is None:
        raise HTTPException(404, "Feature flag not found")
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(403, "Not enough permissions to update this feature flag")
    ...
```

Create is gated by the async dependency `deps.can_create_feature_flag`. The other flag
helpers in `deps.py` are not used by any route.

### Experiment Access (SYNC)

Two synchronous checks in `deps.py`, called from the endpoint once the
experiment is loaded (they are not `Depends` targets). Neither considers who
owns the experiment:

- `get_experiment_read_access(experiment, user)`: a superuser, or a role with
  READ on experiments. The detail, results, daily-results and
  segmented-results routes call it before any cache read.
- `get_experiment_change_access(experiment, user, action=Action.UPDATE)`: a
  superuser, or a role with READ and `action`. Update, schedule, start, pause,
  complete, archive and metadata call it; delete calls it with `Action.DELETE`
  (then refuses anything not in DRAFT); clone calls the read check and then
  checks CREATE.

```python
@router.post("/experiments/{experiment_id}/start")
async def start_experiment(
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if experiment is None:
        raise HTTPException(404, "Experiment not found")
    experiment = deps.get_experiment_change_access(experiment, current_user)
    ...
```

### Permission Check Order

Always check READ before UPDATE/DELETE:

```python
# ✅ CORRECT order (check_permission is synchronous and returns a bool)
if not check_permission(user, ResourceType.EXPERIMENT, Action.READ):
    raise HTTPException(403, "Not authorized to view this resource")
if not check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE):
    raise HTTPException(403, "Not authorized to update this resource")

# ❌ WRONG - checking UPDATE before READ
if not check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE):
    raise HTTPException(403)  # Less informative error
```

## Database Operations

### Schema Configuration

Always set schema environment variables:

```bash
export POSTGRES_DB=experimentation
export POSTGRES_SCHEMA=experimentation
```

### Migration Workflow

```bash
# 1. Update SQLAlchemy model
vim app/models/experiment.py

# 2. Generate migration.  With modules/ present there are two heads, so name
#    the one you extend: the core head id (`alembic heads`) for a core change,
#    `modules@head` for a module's (the file lands in modules/).
alembic revision --autogenerate --head <core head id> -m "add experiment status field"

# 3. CRITICAL: Review generated file
# - Check down_revision points to correct previous migration
# - Verify schema changes match your intent
# - Ensure schema prefix is correct

# 4. Apply migration (from the repository root; `heads`, plural -- a full
#    checkout also has the `modules` branch)
python -m alembic -c backend/app/db/alembic.ini upgrade heads

# 5. Verify
alembic current
alembic history --verbose | head -20
```

### Common Migration Issues

**Multiple heads:** a full checkout has two on purpose — the core chain and the
`modules` branch — so that deleting `modules/` leaves a consistent core chain.
Do **not** merge them: `alembic merge` writes the merge file into the *core*
versions directory with the module head in its `down_revision`, and a core
checkout, which has no such revision, then cannot load the script directory at
all. Apply with `heads` and extend a named head:

```bash
alembic -c backend/app/db/alembic.ini heads     # both, with the `modules` label
alembic -c backend/app/db/alembic.ini upgrade heads
alembic revision --autogenerate --head <core head id> -m "..."   # a core change
alembic revision --autogenerate --head modules@head -m "..."     # a module's
```

**Database/migration mismatch:**
```bash
alembic stamp heads  # Mark current state
```

## Testing Best Practices

### Test Environment Setup

```bash
# ALWAYS activate venv first!
source venv/bin/activate   # from the repository root

# Set test environment
export APP_ENV=test
export TESTING=true

# Run tests
python -m pytest backend/tests/unit/ -v
python -m pytest backend/tests/integration/ -v
python -m pytest backend/tests/ -k "test_feature_name" -v
```

### Test Database Requirements

- PostgreSQL must be running (use Docker: `docker ps | grep postgres`)
- Connection string uses `localhost` on macOS
- Test users must have `hashed_password` set to avoid integrity errors

### Async Test Patterns

```python
import pytest


# For async functions
@pytest.mark.asyncio
async def test_feature_flag_create_permission():
    result = await can_create_feature_flag(current_user=developer_user)
    assert result is True


# For sync functions (a real User: a MagicMock passes check_permission)
def test_experiment_permission(analyst_user, experiment):
    assert get_experiment_read_access(experiment, analyst_user) is experiment
```

### Mocking Authentication

```python
@pytest.fixture
def admin_user(db_session):
    user = User(
        username="admin",
        email="admin@example.com",
        hashed_password="hashed_password_value",  # REQUIRED!
        is_superuser=True,
        role=UserRole.ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def admin_token(admin_user, monkeypatch):
    token = "mock_admin_token"

    # Mock get_current_user
    async def mock_get_current_user():
        return admin_user

    monkeypatch.setattr("backend.app.api.deps.get_current_user", mock_get_current_user)

    # Mock token decoder
    def mock_decode_token(*args, **kwargs):
        return {"sub": str(admin_user.id), "username": admin_user.username}

    monkeypatch.setattr("backend.app.core.security.decode_token", mock_decode_token)

    return token
```

## Pydantic V2 Patterns

```python
from pydantic import BaseModel, field_validator, model_validator, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExperimentSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # Not class Config

    name: str
    status: str

    @field_validator("status")  # Not @validator
    @classmethod
    def validate_status(cls, v):
        if v not in ["DRAFT", "ACTIVE", "PAUSED", "COMPLETED"]:
            raise ValueError("Invalid status")
        return v

    @model_validator(mode="after")  # Not @root_validator
    def validate_dates(self):
        if self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before end_date")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")  # Not class Config
```

## Role-Based Access Control (RBAC)

### User Roles

- **ADMIN**: Full access to all resources
- **DEVELOPER**: Create and manage experiments/feature flags
- **ANALYST**: View all data, cannot create/modify
- **VIEWER**: Read-only access to approved resources

### Permission Checking

```python
from backend.app.core.permissions import (
    Action,
    ResourceType,
    can_act_on_feature_flag,
    check_permission,
)

# check_permission is synchronous in sync and async handlers alike
if not check_permission(user, ResourceType.EXPERIMENT, Action.CREATE):
    raise HTTPException(403, "Not authorized")

# a particular feature flag: the per-flag rule
if not can_act_on_feature_flag(user, flag.owner_id, Action.UPDATE):
    raise HTTPException(403, "Not authorized")
```

### Special Case: Experiment Delete

Do NOT use dependency function for delete endpoint due to status conflicts.
Instead, check permissions inline:

```python
@router.delete("/experiments/{experiment_id}")
def delete_experiment(
    experiment_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    # Get experiment directly
    experiment = db.query(Experiment).filter_by(id=experiment_id).first()

    # Check permissions inline
    check_permission(current_user, "experiment", "DELETE")

    # Verify status
    if experiment.status != ExperimentStatus.DRAFT:
        raise HTTPException(400, "Can only delete DRAFT experiments")

    # Delete
    db.delete(experiment)
    db.commit()
```

## Code Quality Standards

```bash
# Format and auto-fix (ruff replaces black, isort and flake8). The scope is
# backend/ modules/ scripts/ tests/acceptance/ tests/ground_truth/ -- what
# `make format` and the lint job use.
ruff format backend/ modules/ scripts/ tests/acceptance/ tests/ground_truth/
ruff check backend/ modules/ scripts/ tests/acceptance/ tests/ground_truth/ --fix

# Check only, exactly as CI does
ruff check backend/ modules/ scripts/ tests/acceptance/ tests/ground_truth/ && ruff format --check backend/ modules/ scripts/ tests/acceptance/ tests/ground_truth/

# Type checking
mypy backend/app/

# Or use slash command:
/quality fix
```

## Performance Optimization

### Rules Engine Caching

The enhanced rules engine includes:
- **Rule compilation cache**: LRU cache (10,000 entries) for compiled rules
- **Evaluation cache**: Thread-safe LRU cache with TTL for results
- **Metrics collection**: P50, P95, P99 latency tracking

```python
from backend.app.services.rules_evaluation_service import RulesEvaluationService

service = RulesEvaluationService(
    cache_size=1000,  # Evaluation result cache size
    cache_ttl_seconds=300,  # 5 minutes TTL
    metrics_window=1000,  # Sample window for metrics
)

# Evaluate with caching
result = await service.evaluate_rules_cached(
    rules=targeting_rules, context=user_context
)

# Batch evaluation
results = await service.batch_evaluate(users=user_list, rules=targeting_rules)
```

## Common Development Tasks

### Adding New API Endpoint

1. Define Pydantic schema in `app/schemas/`
2. Add route in `app/api/v1/`
3. Implement business logic in `app/services/`
4. Add permission checks using dependencies or inline
5. Write unit tests in `tests/unit/api/`
6. Write integration tests in `tests/integration/`

### Adding New Model

1. Create model in `app/models/`
2. Use fully qualified imports everywhere
3. Generate migration, naming the head it extends (a full checkout has two, so
   a bare `--autogenerate` fails with "Multiple heads are present"):
   `alembic revision --autogenerate --head <core head id> -m "description"`,
   or `--head modules@head` for a module's
4. Review and apply migration
5. Update schemas in `app/schemas/`
6. Add service methods in `app/services/`
7. Write comprehensive tests

### Debugging Tips

- Check logs: `docker-compose logs -f api`
- Use breakpoint() for debugging
- Run specific test: `python -m pytest backend/tests/unit/path/to/test.py::test_name -xvs`
- Clear schema cache in tests: `clear_schema_cache()`
- Verify database state: `psql` into container

## Resources

- API Docs: http://localhost:8000/api/v1/docs
- Health Check: http://localhost:8000/health
- Alembic Docs: https://alembic.sqlalchemy.org/
- FastAPI Docs: https://fastapi.tiangolo.com/
- Pydantic V2: https://docs.pydantic.dev/2.0/
