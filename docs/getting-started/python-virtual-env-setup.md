# Python Virtual Environment Setup Guide

This guide outlines how to set up and configure a Python virtual environment for the Controlvar project.

## The requirements layout

The `requirements/` directory already exists — you do not create it. It holds
four *profiles*, and every one of them resolves to the same pins:

```
requirements/
├── base.txt     # one line: -r ../backend/requirements.txt
├── dev.txt      # base + ipython, debugpy
├── test.txt     # base (every test dependency is already pinned in base)
└── prod.txt     # base + gunicorn
```

**`backend/requirements.txt` is the single source of truth.** It is what the
CI test jobs and the developer venv install. `requirements/base.txt` is a single
`-r ../backend/requirements.txt` line, so the profiles cannot drift from it,
and this page does not list the pins: read the file.

To see what is pinned:

```bash
grep -c '==' backend/requirements.txt
grep '^fastapi' backend/requirements.txt
```

- `grep -c '==' backend/requirements.txt`: how many
- `grep '^fastapi' backend/requirements.txt`: a specific one

Note that `requirements/prod.txt` is **not** what the container installs — the
image installs `backend/requirements/runtime.lock`. The prod profile is for running the
API under gunicorn outside a container.

## Python version

`.python-version` pins 3.11.10, and `pyproject.toml` sets
`requires-python = ">=3.11"`. There is no `setup.py` or `setup.cfg`.

## Editable install

Packaging is declared in `pyproject.toml`
(`[project] name = "experimently-platform"`, with a `[build-system]` table). The name is
not `experimently`: that is the Python SDK under `sdk/python/`.

```bash
pip install -e .
```

## Usage Instructions

### Initial Setup

There is no setup script. It is three commands, from the repository root:

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
```

- `source venv/bin/activate`: on Windows, `venv\Scripts\activate`

### Selecting a profile

```bash
pip install -r requirements/dev.txt
pip install -r requirements/test.txt
pip install -r requirements/prod.txt
```

- `pip install -r requirements/dev.txt`: base + ipython, debugpy
- `pip install -r requirements/test.txt`: base (test deps are pinned in base)
- `pip install -r requirements/prod.txt`: base + gunicorn

ruff (format, import order and lint) is pinned in `backend/requirements.txt`
like everything else, so the version in your venv is the version CI runs — see
[Pre-commit Hooks](pre-commit-hooks.md).
