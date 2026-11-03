"""The ground-truth library (#1544): realistic traffic with known answers.

A seeded generator plans a population and its traffic (``generator``); a driver
sends it through the public API and records every send and every answer in a
ledger (``ledger``, the JSON Lines format other tools parse too); the written
event contract turns the ledger into the exact counts and per-user values the
results must show (``contract``); and an independent reference computes the
statistics from those values (``reference``). ``hashing`` is the assignment
hash, re-implemented.

Every module here imports only the standard library, scipy and statsmodels:
never ``backend``, ``modules``, an HTTP library or the docs runner, so a defect
in the product cannot be copied into the answer it is checked against.
``backend/tests/unit/ground_truth/test_independence.py`` holds that, and the
unit gates beside it hold each module to its contract.

Nothing is re-exported here: import the module you use.
"""
