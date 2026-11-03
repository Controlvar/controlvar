# Testing Guide

How to run the test suites, and the rules a new test follows. The `make` targets
are under "Tests" in
[CONTRIBUTING.md](https://github.com/Controlvar/controlvar/blob/main/CONTRIBUTING.md),
and the conventions behind these rules are in
[CLAUDE.md](https://github.com/Controlvar/controlvar/blob/main/CLAUDE.md).

## Running the backend tests

Run them from the repository root. Activate the virtual environment first, every
time, then set the test environment:

```{.bash skip reason="dev: runs the backend unit tests in your virtualenv"}
source venv/bin/activate
export APP_ENV=test TESTING=true
python -m pytest backend/tests/unit -p no:cov -q
```

To run one file, name it:

```{.bash skip reason="dev: runs one test file in your virtualenv"}
python -m pytest backend/tests/unit/core/test_rules_engine.py -v
```

"Tests" in `CONTRIBUTING.md` says which suites need PostgreSQL, and where they
look for it.

The backend suites are directories under `backend/tests/`, such as `unit`,
`integration` and `smoke`. The pull-request checks run `backend/tests/unit`,
`backend/tests/integration` and `backend/tests/smoke` with no marker filter, so a
test there with no marker still runs. The `e2e` and `contract` suites are run with
`-m e2e` and `-m contract`, so a test there needs that marker.

Run the dashboard's tests from `frontend/`:

```{.bash skip reason="dev: runs the dashboard's jest tests"}
cd frontend && npm test
```

## Markers

Markers are declared in `markers` under `[tool.pytest.ini_options]` in
`pyproject.toml`. Declare a new one there before you use it.

- `@pytest.mark.regression`: every bug fix carries a test with this marker that
  fails on the old code. The `regression-guard` check fails a pull request
  labelled `bug` that changes no test file. Run them with `pytest -m regression`.
- `@pytest.mark.modules`: for a test that needs the modules package. In a core
  build, which has no `modules/`, such a test is skipped.

## Tests and AWS

The repository root's `conftest.py` loads `backend/tests/no_real_aws.py`. The AWS
access keys a test sees are dummies, and the `aws` first on `PATH` is a fake that
fails. A test that needs the AWS CLI puts its own fake `aws` first on `PATH`, and
that one runs.

## Writing a backend test

- Import application code rooted at `backend.app`, as in
  `from backend.app.models.user import User`; the
  [Development Guidelines](guidelines.md) say why.
- Test a permission with a user who is not a superuser, one of each role. A
  superuser passes every permission check.
- [Dependency Injection](dependency-injection.md) says which dependencies are
  async, and so which ones a test awaits.

## Replaying traffic through the API

`tests/ground_truth/test_replay.py` sends seeded, realistic traffic through the
public tracking API to a running Controlvar: four experiments, whose users are
assigned in batches and one at a time and send conversions, repeat purchases and
revenue. It then checks `GET /api/v1/results/{experiment_id}` against what the
documented event contract and an independent reference compute from the
server's own answers.

The Ground-truth Replay workflow runs it on a pull request that changes code the
replay exercises, or one labelled `e2e`; the nightly QA suite runs it with four
times the users. It is not a required check.

To run one scenario against an API you started with `ENVIRONMENT=development` on
an empty database:

```{.bash skip reason="dev: needs a running API started with ENVIRONMENT=development on an empty database"}
GROUND_TRUTH_SEED=1234 python -m pytest tests/ground_truth/test_replay.py -k S2
```

Each run prints its seed at the start, in the GitHub step summary and in every
failure message, with this command filled in. Without `GROUND_TRUTH_SEED` a run
picks a new seed. The run signs in as the first administrator the bootstrap
creates on an empty development database; `GROUND_TRUTH_ADMIN_EMAIL` and
`GROUND_TRUTH_ADMIN_PASSWORD` name another, and `CONTROLVAR_API_URL` another
address than `http://127.0.0.1:8000`.
