# Runnable Documentation Examples

The shell examples on an enrolled page run in CI as they are written, in one bash
session per page. `scripts/doc_examples.py` is the check, and the `doc-examples`
workflow runs it on every pull request. A page fails when a command fails, when an
example stops printing what the page says it prints, or when a block breaks one of
the rules below.

---

## Which pages are checked

Every Markdown page in the repository, except `CLAUDE.md` files and anything under
`.claude/`. The check walks the tree for them, skipping `node_modules`, `venv`,
`site` and hidden directories other than `.github`, and the list it finds must
equal `[meta] universe` in `scripts/doc_examples.toml`. A new page, or a renamed
one, fails the check until that list names it.

On every one of those pages:

- every code fence names a language (`text` for output, a directory tree or
  anything else that is not code);
- a shell fence is `bash`: `sh`, `shell`, `console`, `zsh`, `powershell` and the
  other shells the check knows are refused;
- a page with a `bash` block is exactly one of **enrolled**, **exempt** or
  **pending**.

The pending pages are the ones not yet enrolled. That list can only shrink, and a
pending page's fences that name no language, its shell comment lines and its
`bash -n` failures can only fall.

---

## Tagging a shell block

On an enrolled page, every `bash` block carries exactly one tag, in braces.

**Run it:**

````markdown
```{.bash exec}
curl -s localhost:8000/health/ready | jq .status
```
````

**Don't run it, and say why.** The reason is required, and it starts with a
category (see [Pages that cannot run here](#pages-that-cannot-run-here)):

````markdown
```{.bash skip reason="server: starts a long-running development server"}
cd frontend && npm ci && npm run dev
```
````

`exec` takes an optional `timeout=<seconds>`, from 1 to 3600 (120 when it is not
given), as in `{.bash exec timeout=1200}`.

Any other form of a shell fence on an enrolled page is refused, with the file and
line. Blocks in other languages (`json`, `python`, `javascript`, …) aren't run and
need no tag.

---

## Saying what the reader should see

Put one `expect` line per value directly under the block, with no blank line
between, and also say the value in the prose, because the reader never sees the
`expect` line:

````markdown
```{.bash exec}
curl -s localhost:8000/health/ready | jq .status
```
<!-- expect: "healthy" -->

It prints `"healthy"`.
````

The text of an `expect` line has to appear in what the block prints to stdout, in
the order the lines are given, and a match can't start or end inside a word:
`active` doesn't match `inactive`, and `1` doesn't match `10`. A block that prints
something needs at least one `expect` line, except the two blocks that start a
stack, `docker compose up -d --wait` and
`EXPERIMENTLY_PROFILE=full docker compose up -d --wait`.

Choose a value that would be **different if the step had failed**. `curl -s` exits
0 on a `404` or a `422`, so an example that doesn't check its output passes when
it's broken.

---

## Writing a block that runs as written

- **No placeholders.** A reader can't run `your-api-key` or `flag-uuid-here`.
  Capture the value in an earlier block (`KEY=$(curl … | jq -r .key)`), or mark
  the block `skip`.
- **The page's blocks share one shell, top to bottom.** A variable set in one
  block is there in the next. Nothing from your own environment is: each page runs
  with only `PATH`, `HOME`, `USER`, `TMPDIR` and Docker's own settings.
- **No `#` comments in a shell block**, `skip` blocks included (see
  [Rewriting a comment](#rewriting-a-comment)).
- **Every block passes `bash -n`**, with bash 4.4 or later, the one that runs the
  examples; macOS's `/bin/bash` is 3.2 and is refused. A `skip` block whose reason
  is `fragment: …` is not syntax-checked, since it is a template to fill in.
- **A block that runs may not:**
    - end its last line in `\`, which would swallow what follows;
    - write `export X=$(command)` (or `declare`, `local`, `readonly`), which reports
      success even when the command fails: assign first, then export;
    - run a package manager (`pip`, `npm`, `yarn`, `go get`, `cargo install` and the
      others the check lists), or `aws`, `cdk` or `sam`, however the command is
      written and even inside a here-document. Tag such a block `skip` with the
      `registry` or `aws` category;
    - mention `COMPOSE_PROJECT_NAME` or unset a `COMPOSE_` setting, pass `-p` or
      `--project-name` to `docker compose`, run `env -u` or `env -i`, or remove
      Docker resources directly (`docker rm`, `docker volume rm`, `… prune`). A
      plain `docker compose down -v` is allowed: each page runs under a compose
      project of its own.

---

## Pages that cannot run here

A `skip` reason starts with one of these categories, then `: ` and the words a reader
of the source will see:

| Category | The block… |
|---|---|
| `checkout` | clones or enters the repository (the runner is already inside one) |
| `dev` | is a contributor's command in a development checkout (venv, make, pytest, this repository's own tooling) |
| `server` | starts a long-running process |
| `aws` | needs an AWS account |
| `idp` | needs an identity-provider tenant (Cognito, SAML, OIDC) |
| `secret` | needs a key, a password or a third-party account the reader holds |
| `registry` | installs a package from a public registry; it must name the package manager |
| `toolchain` | needs a language toolchain the runner doesn't use (Go, .NET, JVM, Swift, …) |
| `destructive` | changes state a reader must choose to change (restore, rollback, delete) |
| `demo` | needs the demo applications |
| `fragment` | is a template to fill in, not runnable as written |
| `timing` | gates on latency or throughput, which one run on a shared runner cannot hold: one command, `run_load_tests.py` without `--sla report`, and the page runs the same command with `--sla report` in an `exec` block |
| `bug #N` | fails because of the product defect in issue N: `bug #123: …` |

The check prints how many skipped blocks each category holds.

---

## Rewriting a comment

macOS's default `zsh` doesn't treat `#` as a comment when you paste, so
`jq .status   # "healthy"` fails with `Could not open file #`, and a whole-line
`# step 2` fails with `command not found: #`. `ENV=prod   # or staging` fails the
same way, and leaves `ENV` at whatever it was before. Say it in the sentence before
or after the block instead:

````markdown
Set the environment you are rolling back (`prod` or `staging`):

```{.bash skip reason="aws: rolls back a deployment"}
ENV=prod
```
````

A `#` inside quotes, in the middle of a word (`$#`, `${#x}`, a URL's `#anchor`) or
inside a here-document is not a comment and is fine.

---

## Environments

| Environment | What is true before the page runs |
|---|---|
| `bare` | Docker is available and nothing is running. The page starts its own stack. |
| `stack` | The Quick Start's `docker compose up -d --wait` has already run. |
| `stack-dev` | As `stack`, and the repository root has a development virtual environment, `venv/`, with `backend/requirements.txt` installed, for a contributor's page (`source venv/bin/activate`). A page whose venv cannot `import alembic, locust, backend.app.core.config` fails before its stack starts. |
| `stack-full` | The full profile's `EXPERIMENTLY_PROFILE=full docker compose up -d --wait` has already run, and `GET /api/v1/modules` said `"profile": "full"`. For a page about a module. |
| `local` | Reserved for the demo applications, and refused as not implemented yet. |

---

## Checking a page locally

```{.bash skip reason="dev: checks the documentation itself"}
python scripts/doc_examples.py --check
```

To run the examples, stop anything that holds the stack's ports first (your own
`docker compose stop` is enough). `--only` runs one page:

```{.bash skip reason="dev: runs the documentation's examples"}
python scripts/doc_examples.py --run --only docs/feature-flags/create.md
```

For the render check, build the site with the site's own toolchain first:

```{.bash skip reason="dev: builds the documentation site"}
bash scripts/docs_toolchain.sh && mkdocs build && python scripts/doc_examples.py --render site
```

The render check covers every page under `docs/`, enrolled or not: a fence the site
doesn't show as code fails it on any page.

---

## When the check fails

Every problem is reported in the same run, one line each, naming the page (and the
line, where there is one) and what to change. For a new page with a `zsh` block, and
a comment left in an enrolled one:

```text
refused: docs/guides/new-page.md: a page scripts/doc_examples.toml does not know. Add it to [meta] universe (docs/development/doc-examples.md#enrolling-a-page).
refused: docs/guides/new-page.md:9: 'zsh' names a shell the runner does not know; use {.bash …}
refused: docs/guides/new-page.md: 1 shell block, and the page is not enrolled. Add it to scripts/doc_examples.toml (docs/development/doc-examples.md#enrolling-a-page), or list it as exempt with a reason.
refused: docs/guides/tour.md:14: shell comment "# prints the flag's id" breaks when pasted into zsh. Move it into the sentence before or after the block (docs/development/doc-examples.md#rewriting-a-comment).
4 problems
```

---

## Enrolling a page

1. Add the page's path to `[meta] universe` if it is new.
2. Tag every shell block `exec` or `skip`, remove its comments, and give every fence a
   language.
3. Add `expect` lines, and say the same values in the prose.
4. Run `python scripts/doc_examples.py --check`, and copy the counts it prints into a
   `[[document]]` entry in `scripts/doc_examples.toml`; add its `exec` to `[meta] exec`,
   add one to `[meta] documents`, and remove the page's line from `[pending]` if it has
   one.

A page with no `bash` block needs no entry, and enrolling one is refused.

---

## Exempting a page

A page whose shell blocks are deliberately left untagged is listed as exempt, with a
reason (1–200 characters), instead of enrolled:

```toml
[[exempt]]
path = "docs/example.md"
reason = "why its blocks are not tagged"
```

An exempt page still obeys the rules for every page (languages named, no other shell)
and its `bash` blocks still carry no comments and pass `bash -n`. An exempt page with no
`bash` block is refused.
