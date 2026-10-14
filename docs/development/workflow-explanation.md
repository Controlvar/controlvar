# CI workflows

The checks a pull request has to pass to merge, and how to run them yourself. The
workflows are in `.github/workflows/`.

## Required checks

The checks `main` requires are `REQUIRED_CHECKS` in `scripts/configure-repo.sh`,
the script that writes them to branch protection. Branch protection matches a
required check by its status name, which is the job's `name:`, not its id. So
change a required job's `name:` and that list in the same pull request.

`scripts/check_required_checks.sh` prints each difference between what
`configure-repo.sh` would write and the live branch protection. Run it with your
own `gh` login: the Actions token cannot read branch protection.

## Running a check yourself

- `lint`: see "Lint and format" in the
  [Development Guidelines](guidelines.md#lint-and-format).
- `core-build`: `make core-build`, which runs `scripts/core_build.sh`. It copies
  the tree, deletes `modules/` from the copy, then rebuilds and tests it, and never
  touches your checkout.
- The backend and dashboard suites: see the [Testing Guide](testing-guide.md).
