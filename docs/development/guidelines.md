# Development Guidelines

The rules a change to this repository follows. How to set up, run and test the
stack is in
[CONTRIBUTING.md](https://github.com/Controlvar/controlvar/blob/main/CONTRIBUTING.md),
and the conventions, with the reasons for them, are in
[CLAUDE.md](https://github.com/Controlvar/controlvar/blob/main/CLAUDE.md).

## Python

- Write for Python 3.11, with type annotations.
- Use Pydantic v2: `field_validator`, `model_validator`, `ConfigDict`, and
  `BaseSettings` from `pydantic_settings`.
- Import application code by its full path, rooted at `backend.app`:
  `from backend.app.models.metrics.metric import RawMetric`. Never root an import
  at `app`: Python loads `app.models.metrics.metric` as a different module from
  `backend.app.models.metrics.metric`, with different classes. This search finds
  such an import, and prints nothing when there is none:

    ```{.bash skip reason="dev: searches this repository's Python files"}
    grep -rnE '^\s*(from|import) app\.' backend/ modules/ --include='*.py'
    ```

- A new core model's module goes in `CORE_MODEL_MODULES` in
  `backend/app/models/__init__.py`, which `register_core_models()` imports.

## Lint and format

- Ruff formats and lints the Python and sorts its imports; its configuration is
  `[tool.ruff]` in `pyproject.toml`. Do not add black, isort or flake8 back.
- `make format` formats and fixes the Python in place. `make lint` runs what the
  `lint` check runs.
- CI, the pre-commit hook and the virtual environment install the same ruff
  version.

## Pull requests

- Write the title as a conventional commit, `type(optional scope): description`,
  with `type` one of `feat fix perf deps docs build ci refactor test chore revert
  style`. The `conventional-title` check refuses any other title.
- Sign off every commit and add a regression test to every bug fix:
  `CONTRIBUTING.md` and the [Testing Guide](testing-guide.md) say how.
