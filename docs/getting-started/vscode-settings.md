# VSCode Configuration

`.vscode/` is in `.gitignore`, so these settings are not checked in — this page
is the shared copy. Create the folder and paste the configuration in.

```bash
mkdir -p .vscode && touch .vscode/settings.json
```

The goal is that your editor and the `lint` CI job agree. Where a setting
exists to match a repository rule, the rule is named — if the two ever drift,
`pyproject.toml` and `frontend/eslint.config.mjs` win.

## settings.json

```json
{
  "editor.rulers": [88],
  "editor.formatOnSave": true,
  "editor.renderWhitespace": "boundary",
  "editor.tabSize": 4,
  "editor.indentSize": 4,
  "editor.insertSpaces": true,
  "editor.detectIndentation": false,

  "files.exclude": {
    "**/__pycache__": true,
    "**/.pytest_cache": true,
    "**/*.pyc": true,
    ".mypy_cache": true,
    ".coverage": true,
    "htmlcov": true
  },
  "files.trimTrailingWhitespace": true,
  "files.insertFinalNewline": true,
  "files.trimFinalNewlines": true,

  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
      "source.fixAll.ruff": "explicit",
      "source.organizeImports.ruff": "explicit"
    }
  },

  "python.testing.pytestEnabled": true,
  "python.testing.unittestEnabled": false,
  "python.testing.pytestArgs": ["backend/tests", "--no-cov"],
  "python.analysis.typeCheckingMode": "basic",
  "python.analysis.extraPaths": ["${workspaceFolder}"],

  "[javascript]":      { "editor.tabSize": 2 },
  "[typescript]":      { "editor.tabSize": 2 },
  "[typescriptreact]": { "editor.tabSize": 2 },
  "[json]":            { "editor.tabSize": 2 },
  "[yaml]":            { "editor.tabSize": 2 },

  "eslint.workingDirectories": ["frontend"],
  "eslint.validate": ["javascript", "javascriptreact", "typescript", "typescriptreact"],

  "yaml.schemas": {
    "https://json.schemastore.org/github-workflow.json": ".github/workflows/*.yml"
  },

  "terminal.integrated.env.linux":   { "PYTHONPATH": "${workspaceFolder}" },
  "terminal.integrated.env.osx":     { "PYTHONPATH": "${workspaceFolder}" },
  "terminal.integrated.env.windows": { "PYTHONPATH": "${workspaceFolder}" },

  "autoDocstring.docstringFormat": "google",
  "autoDocstring.startOnNewLine": true
}
```

## Extensions

| extension | id | why |
|---|---|---|
| Python | `ms-python.python` | |
| Pylance | `ms-python.vscode-pylance` | |
| **Ruff** | `charliermarsh.ruff` | format, import order and lint — all three |
| ESLint | `dbaeumer.vscode-eslint` | reads `frontend/eslint.config.mjs` |
| autoDocstring | `njpwerner.autodocstring` | Google-style docstrings |

**Do not install the Black, isort or Flake8 extensions.** Ruff does all three
jobs here (`[tool.ruff]` in `pyproject.toml`); running black alongside it produces
formatting the `lint` job then rejects.

**There is no Prettier in this repository** — no config, no dependency. Do not
set `esbenp.prettier-vscode` as the formatter for JS, TS, JSON or YAML; it will
reformat files that nothing else in the toolchain formats, and the diff is
noise. ESLint is the frontend gate (`--max-warnings 0`), alongside
`tsc --noEmit`.

## Notes

`PYTHONPATH` is set to the workspace root because the package is `backend.app`.
Everything runs from the repository root: `uvicorn backend.app.main:app`, not
`cd backend && uvicorn app.main:app`.

`pytestArgs` points at `backend/tests`. The integration tests need PostgreSQL;
see [Testing](../development/testing-guide.md).

Editor settings are a convenience, not the contract. Before pushing, run
`make lint` — that is what the `lint` job runs.
