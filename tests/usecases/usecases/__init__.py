"""The harness for the over-the-wire use cases (D210 v3: G1-1b, G2-1, G2-2).

* ``proxy``: a recording pass-through between one SDK driver and the API, in
  the runner's process, so the cases count requests on one clock.
* ``runner``: one SDK driver process, spoken to in newline-delimited JSON.
* ``admin``: sign-in and an API key through the ground-truth driver, and the
  flag changes and raw evaluations a case makes, paced under the rate limits.

The drivers themselves are in ``tests/usecases/drivers``. Nothing here imports
``backend``: the cases test a running server over HTTP.
"""
