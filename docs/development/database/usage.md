# Using the Data Layer

Where the database code is, for a contributor writing code against it.

- The API keeps its data in PostgreSQL through SQLAlchemy. The models are in
  `backend/app/models/`, and the optional modules' in `modules/backend/app/models/`.
- The models' tables are in the schema `POSTGRES_SCHEMA` names. When it is not
  set, that is `test_experimentation` with `APP_ENV=test` or `TESTING=true`, and
  `experimentation` otherwise.
- A route's session comes from a dependency: see
  [Route Dependencies](../dependency-injection.md).
- Import a model from the module that defines it, rooted at `backend.app`:
  `from backend.app.models.experiment import Experiment`.
- A script that runs outside the API calls `register_core_models()` from
  `backend.app.models` before its first query, so that SQLAlchemy can configure
  the mappers.
- A change to a model needs a migration: see
  [Guide to Using the SQLAlchemy Models and Database Migrations](migrations.md).
