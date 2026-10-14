# Pre-commit Hooks

`.pre-commit-config.yaml` is **already in the repository root**. You do not
create it — you install it.

```bash
pip install pre-commit
pre-commit install
```

That is the whole setup. To run the hooks over the tree without committing:

```bash
pre-commit run --all-files
```

## What actually runs

Three groups. The file itself is the authority — read it rather than this page
if the two ever disagree.

### File hygiene (`pre-commit/pre-commit-hooks`)

| hook | note |
|---|---|
| `check-yaml` | excludes the Helm chart's templates, which render to YAML but are not YAML |
| `check-json` | excludes `tsconfig*.json` — those are JSONC, and a strict JSON parser is wrong about them |
| `check-added-large-files` | `--maxkb=1024`, for the two OpenAPI snapshots under `docs/api/` |
| `check-merge-conflict` | |
| `debug-statements` | |
| `detect-private-key` | |
| `check-toml` | |
| `check-case-conflict` | |

### Python lint and format (`astral-sh/ruff-pre-commit`)

`ruff --fix` and `ruff-format`, both scoped to
`^(backend|modules|scripts|tests/acceptance)/` — the same four trees as
`make lint` and the `lint` CI job.

**Keep the versions in step.** The hook's `rev:` and the `ruff==` pin in
`backend/requirements.txt` must match, or a commit that passes locally fails
in CI on a formatting difference:

```bash
grep -A1 ruff-pre-commit .pre-commit-config.yaml | grep rev
grep '^ruff==' backend/requirements.txt
ruff --version
```

- `grep -A1 ruff-pre-commit .pre-commit-config.yaml | grep rev`: the hook
- `grep '^ruff==' backend/requirements.txt`: CI and the venv
- `ruff --version`: what you have

### The publication check (`repo: local`)

Two hooks run the repository's publication check: on the staged files at each
commit, and on the whole tree and the outgoing commit messages at each push.

## This is a subset of the gate, not the gate

Pre-commit runs **no frontend checks** — no eslint, no `tsc`. Those live in
`make lint` and in CI.

So a clean `git commit` is not a green pull request. Before pushing:

```bash
make lint
```

- `make lint`: ruff, import-linter, reuse, the lock checks, eslint, tsc, hadolint, actionlint — exactly what the `lint` job runs

## Tips

Bypassing the hooks is `git commit --no-verify`. It only defers the failure to
CI, where it costs a round trip instead of a second.

`pre-commit autoupdate` bumps every `rev:`. Run `make lint` afterwards and
update the matching pin in `backend/requirements.txt` in the same commit — an
autoupdate that moves ruff on its own is precisely the version skew described
above.
