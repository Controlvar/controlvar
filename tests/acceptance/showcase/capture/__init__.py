"""The showcase capture: a walkthrough recorded from a local stack (#1066).

``python -m showcase.capture <slug|all>`` (``make showcase VIDEO=...``) builds
the tree, starts the stack, drives the browser through the storyboard and
writes a capture directory (``showcase.contract``), then runs the render on it.

* ``run``: the invocation, its refusals and the capture directory;
* ``guard``: ports, child environments, the work root, the lock, the disk floor;
* ``build``: the tree from ``git archive``;
* ``procs`` and ``stack``: the children (Postgres, the API, the dashboard);
* ``storyboard``: the YAML, its refusals and the cue list;
* ``narration``: what a ``say:`` sentence may say (G1, G3);
* ``director``: the browser (the only module importing Playwright);
* ``gates``: the gates over recorded numbers.

Everything but ``director`` and ``run``'s browser half is unit tested in
``backend/tests/unit/showcase/``.
"""
