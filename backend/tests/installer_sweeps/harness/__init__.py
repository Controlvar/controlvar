"""The AWS installer beta's offline harness: a stateful fake ``aws`` and its helpers.

Used by the tests in backend/tests/installer_sweeps (Unit Tests and the macOS job),
the static checks in backend/tests/unit/infrastructure, and
scripts/check_installer_cli.py, which compares the fake with the real AWS CLI on a
local stub (CDK Stack Tests). Nothing here reaches AWS.
"""
