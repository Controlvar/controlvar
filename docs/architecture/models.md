# Data Models

The models are SQLAlchemy classes in `backend/app/models/`, one module per area,
and in `modules/backend/app/models/` for the optional modules. Each table is in
the PostgreSQL schema that `POSTGRES_SCHEMA` names.

The fields a client sends and receives are in the API reference at
`/api/v1/docs`, not here.

## Constraints the database enforces

- Variant traffic allocation must be between 0 and 100
- Metric names must be unique within an experiment
- Experiment end date must be after start date
- One assignment per user and experiment
- A feature flag's key is unique, and its rollout percentage is between 0 and 100

The API also checks that an experiment's variant traffic allocations sum to 100.
The database does not check the sum.

## Migrations

- Upgrading a deployment's database: [Database Migrations](../self-hosting/migrations.md)
- Writing a migration in a checkout:
  [Creating New Migrations](../development/database/migrations.md#creating-new-migrations)
