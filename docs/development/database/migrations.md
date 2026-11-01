# Guide to Using the SQLAlchemy Models and Database Migrations

This guide explains how to use the SQLAlchemy models that we've created for the experimentation platform, and how to manage database migrations with Alembic.

## Table of Contents

1. [Project Structure](#project-structure)
2. [Using the SQLAlchemy Models](#using-the-sqlalchemy-models)
3. [Database Migrations with Alembic](#database-migrations-with-alembic)
4. [Working with Experiments](#working-with-experiments)

## Project Structure

The parts of the repository this guide uses:

```text
backend/app/
├── core/config.py            settings, including the POSTGRES_* connection
├── db/
│   ├── alembic.ini           the Alembic configuration every command passes with -c
│   ├── bootstrap.py          the schema of a new database; upgrade heads on one with tables
│   ├── session.py            the engine and SessionLocal
│   └── migrations/
│       ├── env.py            the Alembic environment: connection, schema, both branches
│       ├── script.py.mako    the template of a new revision
│       └── versions/         the core chain's revisions
└── models/                   the SQLAlchemy models, one module per area
    ├── base.py               the base model and its common fields
    ├── experiment.py         Experiment, Variant, ExperimentStatus, ExperimentType
    ├── feature_flag.py
    ├── assignment.py
    ├── event.py
    └── user.py
modules/backend/app/          a full checkout only
├── db/migrations/versions/   the modules branch's revisions
└── models/                   the optional modules' models
```

## Using the SQLAlchemy Models

### Basic Usage

Here's how to use the models in your application code:

```python
from backend.app.db.session import SessionLocal
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant

# Create a database session
db = SessionLocal()

try:
    # Query experiments
    active_experiments = db.query(Experiment).filter(
        Experiment.status == ExperimentStatus.ACTIVE
    ).all()
    
    # Create a new experiment
    new_experiment = Experiment(
        name="Homepage Button Color Test",
        description="Testing different button colors on the homepage",
        hypothesis="A green button will have a higher click-through rate than a blue button",
        status=ExperimentStatus.DRAFT,
        owner_id=user_id
    )
    db.add(new_experiment)
    
    # Add variants to the experiment
    control_variant = Variant(
        experiment_id=new_experiment.id,
        name="Control (Blue)",
        description="The current blue button",
        is_control=True,
        traffic_allocation=50,
        configuration={"color": "blue", "hex": "#0066CC"}
    )
    
    treatment_variant = Variant(
        experiment_id=new_experiment.id,
        name="Treatment (Green)",
        description="The new green button",
        is_control=False,
        traffic_allocation=50,
        configuration={"color": "green", "hex": "#00CC66"}
    )
    
    db.add_all([control_variant, treatment_variant])
    db.commit()
    
except Exception as e:
    db.rollback()
    raise e
finally:
    db.close()
```

### With FastAPI Dependency Injection

In a FastAPI application, you can use dependency injection:

```python
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from backend.app.api.deps import get_db
from backend.app.models.experiment import Experiment

app = FastAPI()

@app.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, db: Session = Depends(get_db)):
    experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment.to_dict()
```

## Database Migrations with Alembic

### Before You Start

Run every command in this section from the repository root, in the development
virtual environment that
[Python Virtual Environment Setup](../../getting-started/python-virtual-env-setup.md)
makes:

```{.bash exec}
source venv/bin/activate
```

Alembic connects with the `POSTGRES_*` settings. Their defaults (`localhost:5432`,
user and password `postgres`, database `experimentation`, schema
`experimentation`) are the PostgreSQL that `make db` starts, which is also the
[Quick Start](../../getting-started/quick-start.md) stack's. Set them only for
another server. CI runs this section as written against the Quick Start stack.

### A New Database

A new database gets its schema from the bootstrap, never from the migrations:
the historical chain cannot be replayed from an empty database. Do not
autogenerate an "initial migration" for one either. Against an empty database
`--autogenerate` writes a revision that creates every table again, beside the
chain that already does. The bootstrap creates the schema from the models and
records the heads. On a database that already has tables it runs `upgrade heads`
instead, so it is safe to run again:

```{.bash exec}
ENVIRONMENT=development python -m backend.app.db.bootstrap
```
<!-- expect: Database bootstrap complete -->

It ends with `Database bootstrap complete (created)` on a new database and
`Database bootstrap complete (upgraded)` on one that had tables.
`ENVIRONMENT=development` lets it create the first administrator with the
development default password (`FIRST_SUPERUSER`, `FIRST_SUPERUSER_PASSWORD`) when
the database has no users.

The Quick Start stack runs the core images, so the database it makes has the
core chain only. A full checkout's bootstrap against it also applies the
`modules` branch and creates the module tables, and logs a WARNING saying so
(see [Switching Profile](../../self-hosting/migrations.md#switching-profile)).

A full checkout has two heads, the core chain and the `modules` branch, and
`heads` labels the second:

```{.bash exec}
alembic -c backend/app/db/alembic.ini heads
```
<!-- expect: (modules) (head) -->

The database records one row per head:

```{.bash exec}
alembic -c backend/app/db/alembic.ini current
```
<!-- expect: (head) -->

### Creating New Migrations

Whenever you make changes to the models, generate a new migration script. Name
the head the revision extends: the core head is the line of `heads` without
`(modules)`, and a module's change extends `modules@head`. The file lands next to
that head. Without `--head`, alembic refuses with "Multiple heads are present".
Never answer that with `alembic merge`: the merge file lands in the core chain
with the module head in its `down_revision`, and a core checkout then cannot
load its migrations at all.

```{.bash skip reason="fragment: <core head id> is the core line that alembic heads prints"}
alembic -c backend/app/db/alembic.ini revision --autogenerate \
        --head <core head id> -m "Description of changes"
```

Autogenerate compares the models with the database, so run it against a database
at the heads. Its file is a starting point, not a finished migration. Open and
review it before applying it:

```python
# Example generated migration
def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('email_verified', sa.Boolean(), nullable=True),
        schema='experimentation'
    )

def downgrade() -> None:
    op.drop_column('users', 'email_verified', schema='experimentation')
```

Check for:

- a `down_revision` that is the revision ID of the head it extends;
- the schema name (`schema='experimentation'`) on every operation;
- no table dropped, and no data lost, that you did not mean;
- the data types and constraints you meant.

Apply the migration:

```{.bash exec}
python -m alembic -c backend/app/db/alembic.ini upgrade heads
```

`alembic revision` runs `backend/app/db/migrations/env.py` here (`revision_environment = true`
in `alembic.ini`), so it needs the same database connection `--autogenerate`
does. A core checkout has one head and needs no `--head`.

### Migration Commands Reference

Upgrade to the latest version:

```{.bash exec}
python -m alembic -c backend/app/db/alembic.ini upgrade heads
```

Downgrade the previous revision of one branch. With two heads a bare `downgrade -1` is ambiguous -- alembic warns and picks one -- so name the branch: `modules@-1` for a module's, the revision id for a core one. NOT `modules@base`: modules_0001_rbac is a child of core a7b8c9d0e1f2, not an alembic base, and `modules@base` resolves to the whole core chain, every table dropped:

```{.bash skip reason="destructive: unapplies the newest modules revision and drops what it added"}
alembic -c backend/app/db/alembic.ini downgrade modules@-1
```

Downgrade to a specific version:

```{.bash skip reason="fragment: <revision> is a revision id from alembic history"}
alembic -c backend/app/db/alembic.ini downgrade <revision>
```

Show current version (one row per head):

```{.bash exec}
alembic -c backend/app/db/alembic.ini current
```
<!-- expect: (head) -->

Show migration history, newest first, down to the first revision (`<base> -> ...`):

```{.bash exec}
alembic -c backend/app/db/alembic.ini history
```
<!-- expect: (modules) (head) -->
<!-- expect: <base> -->

The modules branch has three revisions, `modules_0001_rbac`,
`modules_0002_warehouse_analysis` and then `modules_0003_lower_is_better`:
`downgrade modules@-1` unapplies `modules_0003_lower_is_better` only, and
`downgrade modules@-3` the whole branch. To stop anywhere in between, name the
revision to end at, such as `downgrade modules_0002_warehouse_analysis`: a
count in between names a different revision once the branch grows.

## Working with Experiments

### Creating an Experiment

```python
from backend.app.models.experiment import Experiment, ExperimentStatus, ExperimentType, Variant
from uuid import UUID

def create_experiment(db, name, description, hypothesis, owner_id, variants_data):
    """
    Create a new experiment with variants.
    
    Args:
        db: SQLAlchemy session
        name: Experiment name
        description: Experiment description
        hypothesis: Experiment hypothesis
        owner_id: User ID of experiment owner
        variants_data: List of variant configurations
    
    Returns:
        Created experiment object
    """
    # Create experiment
    experiment = Experiment(
        name=name,
        description=description,
        hypothesis=hypothesis,
        status=ExperimentStatus.DRAFT,
        experiment_type=ExperimentType.A_B,
        owner_id=UUID(owner_id) if isinstance(owner_id, str) else owner_id
    )
    db.add(experiment)
    db.flush()  # Flush to get the experiment ID
    
    # Create variants
    variants = []
    for variant_data in variants_data:
        variant = Variant(
            experiment_id=experiment.id,
            name=variant_data["name"],
            description=variant_data.get("description", ""),
            is_control=variant_data.get("is_control", False),
            traffic_allocation=variant_data.get("traffic_allocation", 50),
            configuration=variant_data.get("configuration", {})
        )
        variants.append(variant)
    
    db.add_all(variants)

    return experiment
```
