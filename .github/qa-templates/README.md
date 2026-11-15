# QA output templates

The fixed wording of everything the automated QA workflows post where anyone can
read it: the synthetic check's issue and comments, the API fuzzing summaries and
issue, the docs-journey summaries and per-guide report headers, and the
docs-defect issue. This repository is public, so every workflow log, step
summary, issue and comment written from here is public the moment it is posted,
and editing it afterwards does not recall the notification e-mail.

`backend/tests/unit/infrastructure/test_qa_templates_public_text.py` reads every
file in this directory on every pull request (see "The test" below).

## Why here

- Every workflow that posts one of these reads it from its own checkout of this
  repository; nothing has to be packaged or copied.
- Not `.github/ISSUE_TEMPLATE/`: GitHub offers every Markdown file there on the
  "New issue" page.
- Not under `docs/`: the site would publish them, and a pull request that
  changes only `docs/` is classed documentation-only by
  `scripts/classify_changes.py`, so the unit job that runs the test above would
  only report. A change here runs the full gate.

## Format

- One `.tmpl` file per output. Its text is Markdown, which GitHub renders when
  it is posted (issue bodies, comments, step summaries), so lines meant to
  stand apart are separated by a blank line. It is not named `.md` because it
  is not a documentation page: every Markdown file in the repository belongs to
  the documentation examples contract (`[meta] universe` in
  `scripts/doc_examples.toml`), and this README is the one page here that is
  listed there.
- A file whose name ends in `-issue.tmpl` is an issue: line 1 is the title,
  line 2 is blank, and the rest is the body.
- A placeholder is a name in braces, such as `{date}`, and braces appear
  nowhere else. A workflow renders a template by replacing each placeholder
  with its value from one fixed mapping of name to value, and refuses a
  placeholder it has no value for. It never renders one through a shell, a
  heredoc or `envsubst`: those expand whatever variable a template names, and
  the runner's environment holds the credentials.
- The QA workflows post only rendered templates; the workflow pin in
  `backend/tests/unit/infrastructure/test_qa_templates_public_text.py`
  enforces which workflows may post inline.

## Placeholders

Only these names may appear. Each is one of the kinds the QA plan allows in
public text (date, commit, run link, check name, step, HTTP status, count,
seed, duration, and for the docs runs the guide path, step number and
heading). A value is never a response or request body, a key or any part of
one, a token, a user or experiment id, an e-mail address, or a hostname.

| Placeholder | Kind | Value |
|---|---|---|
| `{date}` | date | the UTC date, `2026-10-06` |
| `{date_time}` | date | the UTC date and minute, `2026-10-06 14:17 UTC` |
| `{sha}` | commit | a commit of this repository, full or abbreviated |
| `{run_link}` | run link | the workflow run's URL on github.com |
| `{check}` | check name | synthetic: `staging` or `production`; fuzzing: the pass, `superuser`, `viewer` or `sdk` |
| `{step}` | step | the synthetic check's step, `1` to `8` |
| `{status}` | HTTP status | a status code such as `503`, or `no answer` |
| `{seed}` | seed | the fuzzing seed, an integer |
| `{duration}` | duration | how long a failure lasted, `2 h 30 min` |
| `{count_runs}` | count | runs in a row that failed |
| `{count_fuzzed}` | count | operations fuzzed in this pass |
| `{count_operations}` | count | operations this pass selects (the served API document minus the pass's exclusions) |
| `{count_5xx}` | count | operations that answered 5xx |
| `{count_5xx_unlisted}` | count | of those, operations not in the known list |
| `{count_known_quiet}` | count | known-list entries whose operation answered no 5xx |
| `{count_reached}` | count | operations reached |
| `{count_unreached}` | count | operations not reached, every one listed with a reason |
| `{count_unreached_unlisted}` | count | operations not reached and not listed |
| `{count_unreached_stale}` | count | operations listed as not reached that were reached |
| `{count_pass}` | count | guides that pass |
| `{count_fail}` | count | guides that fail |
| `{count_partial}` | count | guides with steps not run |
| `{count_nav}` | count | pages in the `mkdocs.yml` nav |
| `{count_not_covered}` | count | nav pages with no journey |
| `{count_steps}` | count | steps in a guide |
| `{count_steps_pass}` | count | steps of a guide that pass |
| `{count_not_run}` | count | steps of a guide not run |
| `{guide_path}` | guide path | the page's path in the repository, as `mkdocs.yml` names it, or the path of the dashboard component a journey walks (`.tsx`) |
| `{step_number}` | step number | the guide step's number |
| `{heading_guide}` | heading | the guide's title, its first heading |
| `{heading_step}` | heading | the guide step's own heading, then its id in parentheses (the docs journeys: `Home page (nav-pages)`), because steps can share one heading |

A new placeholder is added to the test's `PLACEHOLDERS` and to this table in
the same pull request, and its name starts with its kind.

## The templates

| File | Posted by | Where |
|---|---|---|
| `synthetic-issue.tmpl` | the synthetic check | the issue opened after repeated failures (rules below) |
| `synthetic-step-changed.tmpl` | the synthetic check | a comment on that issue when the failing step changes |
| `synthetic-recovered.tmpl` | the synthetic check | the comment that closes it |
| `fuzz-green.tmpl` | each fuzzing pass | the step summary of a green pass |
| `fuzz-red.tmpl` | each fuzzing pass | the step summary of a red pass, and each comment on the open fuzzing issue |
| `fuzz-issue.tmpl` | API fuzzing | the issue opened on the first red pass; its body is `fuzz-red.tmpl` |
| `docs-run-green.tmpl`, `docs-run-red.tmpl` | the docs journeys | the top of the run's step summary |
| `docs-guide-pass.tmpl`, `docs-guide-fail.tmpl`, `docs-guide-partial.tmpl` | the docs journeys | the top of each guide's report; pass or partial is also the comment that closes the guide's issue |
| `docs-journey-issue.tmpl` | the docs journeys | one issue per failing guide (rules below); its body is `docs-guide-fail.tmpl`, and a comment when the failing step changes |
| `docs-journeys-alarm-issue.tmpl` | the docs journeys | one issue when a run's report did not decide for every guide (rules below); its body is also each comment on it |
| `docs-defect-issue.tmpl` | the docs journeys | one issue per measured docs defect (rules below) |

Each output says what it is in its first two lines. Its first line carries its
own verdict word (GREEN, RED, PASS, FAIL, PARTIAL, "is red", "fails"), which the
test pins per template; the first two lines carry "What to do" or a count, and
every output that is not green says "What to do" there. For an issue, the title
carries the verdict and the body's first two lines carry the rest.

## The synthetic check's issue

The synthetic workflow follows these rules; the issue body states them for the
reader.

1. One issue per target, labelled `synthetic-failure`, opened from
   `synthetic-issue.tmpl` after 3 failed runs in a row on staging, or 2 on
   production. Never one per run.
2. While it is open, a comment (`synthetic-step-changed.tmpl`) is added only when
   the failing step differs from the step the issue or its latest comment
   names. A failure at the same step adds nothing.
3. The first run that passes comments `synthetic-recovered.tmpl` and closes the
   issue.
4. A run that is skipped (`SYNTH_ENABLED` not `true`) counts as neither a
   failure nor a pass: it does not break a run of failures, and it does not
   add to one. The same holds for a run that was cancelled, or whose check job
   failed before the check itself started (a checkout that failed, say).
5. A run counts as a pass only when its check job succeeded and its step
   `ran 8 of 8` ran, which it does only when the check reports all eight steps
   passed. The count of failed runs in a row, and when the first of them
   started, come from the workflow's own run history on `main`.
6. Every failed run also writes the issue's body, as it would be posted, to the
   run's step summary; the run that closes the issue writes its closing
   comment there.

`scripts/synthetic_report.py` applies these rules and
`backend/tests/unit/scripts/test_synthetic_report.py` pins them.

## The docs journeys' issues

`docs-journeys.yml` follows these rules; `scripts/docs_journeys_report.py`
applies them and `backend/tests/unit/scripts/test_docs_journeys_report.py`
pins them.

1. One issue per failing guide, labelled `docs-journey-failure`, opened from
   `docs-journey-issue.tmpl` when the guide has failed in 2 scheduled runs in a
   row. The runs are nightly: one red night can be the site or the network
   for a moment, and a third night would add a day before anyone hears.
2. While it is open, a comment (the issue's body, rendered for the new run) is
   added only when the guide's failing step differs from the step the issue
   or its latest comment names.
3. The first run in which the guide passes, or runs with nothing failing but
   some steps not run, comments the guide's report header from that run
   (`docs-guide-pass.tmpl` or `docs-guide-partial.tmpl`) and closes the issue.
4. A run that wrote no verdicts (the journeys did not start), or in which the
   guide did not run, counts as neither a failure nor a pass. A run whose
   verdicts are there but cannot be downloaded or read is not passed over:
   the report stops (exit 1) and names that run, and the alarm below is
   raised.
5. Only a scheduled run on `main` posts. Any other run (a dispatch, a release
   run, a run on another branch) prints in its log what a scheduled run with
   its results would post, and posts nothing.

## The docs journeys' alarm

`docs-journeys.yml`'s `report-alarm` job follows these rules;
`scripts/docs_journeys_report.py alarm` applies them, and
`backend/tests/unit/scripts/test_docs_journeys_report.py` and
`test_docs_journeys_workflow.py` pin them.

1. A run is healthy only when its report job succeeded and both the journeys
   job and the demo job wrote verdicts. Anything else (the report job failed,
   was cancelled or did not run, or a job wrote no verdicts) means the report
   did not decide for every guide, so a failing guide may have no issue.
2. One alarm issue, labelled `docs-journey-failure`, opened from
   `docs-journeys-alarm-issue.tmpl` by the first scheduled run that is not
   healthy. It is found by its exact title and the workflow's own author,
   never by the label alone, so the alarm never comments on or closes a
   guide's issue.
3. While it is open, each scheduled run that is not healthy comments the
   issue's body, rendered for that run.
4. The first healthy scheduled run closes it, without a comment.
5. Only a scheduled run on `main` opens, comments on or closes it. Any other
   run prints in its log what a scheduled run would do. A run cancelled as a
   whole runs no alarm.

## The API fuzzing issue

`fuzz.yml` follows these rules; `scripts/fuzz_report.py` applies them and
`backend/tests/unit/scripts/test_fuzz_report.py` pins them.

1. One issue, labelled `fuzz-failure`, opened from `fuzz-issue.tmpl` by the
   first red pass of a scheduled run when none is open; its body is that
   pass's `fuzz-red.tmpl` summary. The run's other red passes comment their
   own summary on it. A red pass means a 5xx or an unreached operation that no
   list explains, or a list entry that went stale, so one red night is enough.
2. While it is open, every red pass of a scheduled run comments its
   `fuzz-red.tmpl` summary.
3. Nothing closes it automatically: it is closed by hand once the cause is
   fixed, or listed in `tests/fuzz/known-5xx.toml` or
   `tests/fuzz/unreached.toml` after triage.
4. A green pass, and a pass with no verdict (its job failed or was cancelled
   before the evaluation), post nothing.
5. Only a scheduled run on `main` posts. Any other run (a dispatch, a run on
   another branch) prints in its log what a scheduled run with its results
   would post, and posts nothing.

## Rendering

`scripts/qa_render.py` is the one reader of this directory. It fills a
template's placeholders from one fixed mapping of name to kind, checks every
value against its kind with an anchored pattern (a commit is 7 to 40 hex
digits; a run link is a run of this repository; a check name, a step and an
HTTP status come from fixed sets; counts and seeds are bounded digits; a
heading is one line of plain text with no URL), refuses a placeholder it has no
value for and a value the template does not use, renders in one pass, and
refuses output that is not ASCII. For an issue it writes the title (the
rendered first line) and the body to separate files; the workflow passes the
title's first line to `--title` and the body to `--body-file`.
`backend/tests/unit/scripts/test_qa_render.py` plants a bad value of every
kind.

## The docs-defect issue

Filed only after the same step has failed in two runs. One defect per issue: a
second failing step in the same guide is a second issue, unless it was NOT RUN
because an earlier step failed. Labels: `documentation` and `qa-agent`, plus
`launch-blocking` for a guide with a release recording, `post-launch` for any
other.

## Labels

No label is created by a pull request. A workflow creates its label the first
time it needs one, the way `nightly-qa.yml` creates `nightly-failure`
(`gh label create ... || true`), with the colour and description below.

| Label | Colour | Description | Applied to |
|---|---|---|---|
| `synthetic-failure` | `B60205` | Synthetic check failed | `synthetic-issue.tmpl` |
| `fuzz-failure` | `B60205` | API fuzzing failed | `fuzz-issue.tmpl` |
| `docs-journey-failure` | `B60205` | Docs journey failed | `docs-journey-issue.tmpl`, `docs-journeys-alarm-issue.tmpl` |
| `qa-agent` | `5319E7` | Filed by automated QA after reproduction | `docs-defect-issue.tmpl`, and public bugs filed from QA findings |

`documentation`, `launch-blocking` and `post-launch` already exist. No
`stats-failure` label is needed: the statistics batteries run in the unit job of
every pull request and of the nightly run.

## The test

`test_qa_templates_public_text.py` fails when:

- this directory holds a file the test does not list, or lacks one it does;
- a template uses a placeholder outside the table above, a brace that is not a
  placeholder, or another placeholder syntax (a dollar sign, angle brackets,
  printf-style);
- a template's placeholders differ from the set the test pins for it, title and
  body separately for an issue;
- a template holds a character outside ASCII;
- a template or this file contains an e-mail address outside the example.com
  domain, an `@` mention, a UUID, a run of seven or more digits, anything shaped
  like a key, token or credential header, a fenced code block, a URL other than
  this repository's or its documentation site's, a hostname, the name of a
  private repository or of another repository of the organisation, or one of
  the words the project keeps out of automated public text;
- a template's first line lacks its pinned verdict word, or its first two lines
  break the rule above;
- the synthetic issue does not list exactly the eight steps, or an issue's body
  differs from its source (`fuzz-red.tmpl`, `docs-guide-fail.tmpl`);
- a workflow or composite action under `.github/` posts to an issue or pull
  request (`gh issue` or `gh pr` create, comment, edit, close, reopen or review)
  or writes its step summary, and is not classified in the test's
  `WORKFLOW_POSTERS`: either `template` (every such command uses `--body-file`,
  never `--body`, and the file renders from this directory) or `legacy-inline`,
  the named workflows that built their text inline before these templates
  existed. An entry that no longer posts must leave the list.

Each of those refusals is also planted in the test itself, so a rule that stops
firing fails too.
