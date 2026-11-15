## What changed

<!-- One or two sentences. What does this PR do, and why? -->

## How it was verified

<!-- The commands you actually ran and what they printed. For example:
     `make test-unit` → 4185 passed
     `npx playwright test --project=journeys` → 5 passed
     manual: logged in, created an experiment, saw it on /experiments -->

- [ ] Tests run locally and green
- [ ] `make lint` clean (`ruff check`, `ruff format --check`, `eslint`, `tsc`)

## Regression test

<!-- Required for anything labelled `bug`. Name the test and say, in one line,
     how it fails on the old code. A fix without a test that would have caught
     the bug is a fix that comes back. Mark it `@pytest.mark.regression`
     (backend) so the guard can find it. -->

- [ ] This is a bug fix, and it adds a test that fails without the fix:
      `<path::test_name>`
- [ ] Not a bug fix (or the fix is untestable — say why here, in the
      description; a maintainer decides whether to waive the check and adds
      the waiver label)

## Risk and rollback

<!-- Anything that touches auth, migrations, the SDK contract or a public API:
     say what breaks if this is wrong and how to undo it. -->

- [ ] No migration, or every migration here is expand-only: it drops or renames
      nothing the previous release's code uses (that waits for a later release)
