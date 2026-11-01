"""Harness: a PUBLISHED ``controlvar`` wheel's public client in local mode,
against a running backend.

    python -I -S published_python_live.py <dir> <config JSON>

``<dir>`` is the verified extraction, the only directory put on ``sys.path``
(``-I -S``: no environment variables, no user or site packages, no script
directory), so the ``controlvar`` imported is the published one or nothing.
``tests/sdk-contract/live/run_live_contract.py`` runs it and does every
comparison; this only reports what the client answered.

It prints one JSON line once the client has loaded the ruleset and answered
every flag (``loaded``), then waits for one line on stdin (the runner has
turned the flags off through the API), then evaluates every flag until each
answers ``inactive`` locally or the wait is over, and prints a second line
(``after``).
"""

import json
import sys
import time


def main(argv):
    root, config = argv[1], json.loads(argv[2])
    sys.path.insert(0, root)
    import controlvar
    import controlvar.local as local
    from controlvar.version import __version__

    errors = []
    client = controlvar.ExperimentationClient(
        config["api_url"],
        config["api_key"],
        evaluation="local",
        refresh_interval_seconds=config["refresh_seconds"],
        on_error=lambda error, operation: errors.append(f"{operation}: {error}"),
    )

    def answers():
        out = {}
        for flag in config["flags"]:
            result = client.get_feature_flag(flag, config["user_id"])
            out[flag] = {
                "enabled": result.enabled,
                "reason": result.reason,
                "source": result.source,
            }
        return out

    def status():
        current = client.status()
        return {"ready": current.ready, "version": current.ruleset_version}

    def emit(report):
        sys.stdout.write(json.dumps(report) + "\n")
        sys.stdout.flush()

    ready = client.ready(timeout_seconds=config["ready_seconds"])
    modules = sorted(
        {
            module.__file__
            for name, module in list(sys.modules.items())
            if (name == "controlvar" or name.startswith("controlvar."))
            and getattr(module, "__file__", None)
        }
    )
    emit(
        {
            "phase": "loaded",
            "identity": {
                "package": "controlvar",
                "version": __version__,
                "local_module": local.__file__,
                "package_file": controlvar.__file__,
                "modules": modules,
                "python": sys.version.split()[0],
            },
            "ready": {"ok": True}
            if ready
            else {"ok": False, "error": str(ready.error)},
            "status": status(),
            "answers": answers(),
            "errors": list(errors),
        }
    )

    sys.stdin.readline()
    started = time.monotonic()

    def off(found):
        return all(
            not a["enabled"] and a["reason"] == "inactive" and a["source"] == "local"
            for a in found.values()
        )

    after = answers()
    while not off(after) and time.monotonic() - started < config["wait_seconds"]:
        time.sleep(0.25)
        after = answers()
    emit(
        {
            "phase": "after",
            "seconds": time.monotonic() - started,
            "status": status(),
            "answers": after,
            "errors": list(errors),
        }
    )
    client.close()


if __name__ == "__main__":
    main(sys.argv)
