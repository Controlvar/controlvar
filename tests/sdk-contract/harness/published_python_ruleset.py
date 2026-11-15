"""Harness: a PUBLISHED ``controlvar`` wheel's own local evaluator.

    python -I -S published_python_ruleset.py vectors  <dir> <config JSON>
    python -I -S published_python_ruleset.py sequence <dir> '{"plan": "<plan file>"}'

``<dir>`` is the verified extraction, the only directory put on ``sys.path``
(``-I -S``: no environment variables, no user or site packages, no script
directory), so the ``controlvar`` imported is the published one or nothing. It
runs in its own process, from an empty working directory, and prints one JSON
report; ``tests/sdk-contract/published_sdks.py`` and the integration test do
every comparison.

``vectors`` (G1): the package's ``LocalEvaluation`` is driven by a stub
``send`` through three documents -- a baseline holding only the sentinel, on;
the vectors' ``ruleset`` plus the sentinel, on; the same with the sentinel
off -- and its status and answer for the sentinel are reported after each.
Every case is then answered by the package's own ``index_ruleset`` and
``evaluate_locally`` over the second document, as the SDK's own tests do.

``sequence`` (G2): the plan lists response bodies captured from
``GET /api/v1/sdk/ruleset``; each is served, in order, to one ``refresh_once``
and the plan's probes are answered after it. The plan's ``server`` part replays
captured responses to an ``ExperimentationClient`` in server mode.
"""

import json
import sys
from urllib.parse import urlsplit


def _load(root):
    sys.path.insert(0, root)
    import controlvar
    import controlvar.evaluator as evaluator
    import controlvar.local as local
    from controlvar.transport import Response
    from controlvar.version import __version__

    modules = sorted(
        {
            module.__file__
            for name, module in list(sys.modules.items())
            if (name == "controlvar" or name.startswith("controlvar."))
            and getattr(module, "__file__", None)
        }
    )
    identity = {
        "package": "controlvar",
        "version": __version__,
        "local_module": local.__file__,
        "package_file": controlvar.__file__,
        "modules": modules,
        "python": sys.version.split()[0],
    }
    return controlvar, evaluator, local, Response, identity


def _shown(evaluator, answer):
    if answer is evaluator.DEFER:
        return "DEFER"
    return {"enabled": answer["enabled"], "reason": answer["reason"]}


def _vectors(root, config):
    _, evaluator, local, Response, identity = _load(root)

    def read():
        with open(config["vectors"], encoding="utf-8") as handle:
            return json.load(handle)

    sentinel_on = {
        "key": config["sentinel"],
        "active": True,
        "evaluation": "local",
        "rollout_percentage": 100,
        "rules": [],
        "default_rule": None,
    }
    sentinel_off = {"key": config["sentinel"], "active": False}

    def with_sentinel(flag, version=None):
        ruleset = read()["ruleset"]
        doc = dict(ruleset, flags=list(ruleset["flags"]) + [flag])
        if version is not None:
            doc["version"] = version
        return doc

    documents = [
        (
            "baseline",
            {
                "schema": evaluator.RULESET_SCHEMA,
                "version": config["baseline_version"],
                "bucketing": evaluator.BUCKETING,
                "flags": [sentinel_on],
            },
        ),
        ("server", with_sentinel(sentinel_on)),
        ("sentinel-off", with_sentinel(sentinel_off, config["off_version"])),
    ]
    current = {}
    reports = []

    def send(method, path, headers, body):
        if method == "GET" and path == local.RULESET_PATH:
            # No ETag: the runtime then keys its next request on the version.
            return Response(200, {}, json.dumps(current["doc"]).encode("utf-8"))
        return Response(202, {}, b"{}")

    def report(error, operation):
        reports.append({"operation": operation, "message": str(error)})

    runtime = local.LocalEvaluation(send, report, 30.0, start=False)
    steps = []
    for label, doc in documents:
        current["doc"] = doc
        runtime.refresh_once()
        status = runtime.status()
        steps.append(
            {
                "label": label,
                "version": status.ruleset_version,
                "ready": status.ready,
                "error": str(status.last_error) if status.last_error else None,
                "sentinel": _shown(
                    evaluator,
                    runtime.answer(config["sentinel"], config["probe_user"], None),
                ),
            }
        )

    server = documents[1][1]
    refused = []
    for flag in server["flags"]:
        alone = {
            "schema": evaluator.RULESET_SCHEMA,
            "version": "one-flag",
            "bucketing": evaluator.BUCKETING,
            "flags": [flag],
        }
        if evaluator.index_ruleset(alone) is None:
            refused.append(
                str(flag["key"])
                if isinstance(flag, dict) and "key" in flag
                else json.dumps(flag)
            )

    cases = read()["cases"]
    indexed = evaluator.index_ruleset(server)
    answers, index_error = None, None
    if indexed is not None:
        answers = [
            _shown(
                evaluator,
                evaluator.evaluate_locally(
                    indexed, c["flag"], c["user_id"], c["context"]
                ),
            )
            for c in cases
        ]
    elif evaluator.is_supported_format(server):
        index_error = "index_ruleset refused the document"
    else:
        index_error = (
            "a format this SDK does not understand (schema "
            f"{json.dumps(server.get('schema'))}, bucketing {json.dumps(server.get('bucketing'))})"
        )
    runtime.close()
    return {
        "identity": identity,
        "steps": steps,
        "refused_flags": refused,
        "answers": answers,
        "index_error": index_error,
        "reports": reports,
    }


def _sequence(root, plan_path):
    controlvar, evaluator, local, Response, identity = _load(root)
    with open(plan_path, encoding="utf-8") as handle:
        plan = json.load(handle)

    current = {}
    requests = []

    def send(method, path, headers, body):
        if method == "GET" and path == local.RULESET_PATH:
            requests.append({"if_none_match": dict(headers).get("If-None-Match")})
            step = current["step"]
            with open(step["body_file"], "rb") as handle:
                payload = handle.read()
            return Response(step.get("status", 200), {"ETag": step["etag"]}, payload)
        return Response(202, {}, b"{}")

    reports = []

    def report(error, operation):
        reports.append({"operation": operation, "message": str(error)})

    runtime = local.LocalEvaluation(send, report, 30.0, start=False)
    steps = []
    for step in plan["steps"]:
        current["step"] = step
        before = len(requests)
        runtime.refresh_once()
        status = runtime.status()
        steps.append(
            {
                "version": status.ruleset_version,
                "ready": status.ready,
                "error": str(status.last_error) if status.last_error else None,
                "if_none_match": requests[before]["if_none_match"]
                if len(requests) > before
                else None,
                "server_evaluated_flags": status.server_evaluated_flags,
                "probes": [
                    _shown(
                        evaluator,
                        runtime.answer(p["flag"], p["user_id"], p.get("context")),
                    )
                    for p in step["probes"]
                ],
            }
        )
    runtime.close()

    server = plan.get("server")
    server_report = None
    if server:
        replies = server["replies"]
        seen, unmatched = [], []

        class Replay:
            def send(self, method, url, headers, body, timeout):
                parts = urlsplit(url)
                target = parts.path + (f"?{parts.query}" if parts.query else "")
                seen.append(f"{method} {target}")
                reply = replies.get(f"{method} {target}")
                if reply is None:
                    unmatched.append(f"{method} {target}")
                    return Response(404, {}, b'{"detail": "not captured"}')
                with open(reply["body_file"], "rb") as handle:
                    payload = handle.read()
                return Response(
                    reply["status"], {"Content-Type": "application/json"}, payload
                )

        client = controlvar.ExperimentationClient(
            server["api_url"], server["api_key"], transport=Replay()
        )
        results = []
        for call in server["calls"]:
            try:
                if call["op"] == "get_feature_flag":
                    flag = client.get_feature_flag(call["flag"], call["user_id"])
                    results.append({"enabled": flag.enabled, "reason": flag.reason})
                elif call["op"] == "is_feature_enabled":
                    results.append(
                        client.is_feature_enabled(call["flag"], call["user_id"])
                    )
                elif call["op"] == "get_all_flags":
                    results.append(client.get_all_flags(call["user_id"]))
                else:
                    results.append({"error": f"unknown op {call['op']}"})
            except Exception as exc:  # reported; the test judges it
                results.append({"error": f"{type(exc).__name__}: {exc}"})
        server_report = {"results": results, "requests": seen, "unmatched": unmatched}

    return {
        "identity": identity,
        "steps": steps,
        "reports": reports,
        "server": server_report,
    }


def main(argv):
    mode, root, argument = argv[1:4]
    config = json.loads(argument)
    if mode == "vectors":
        report = _vectors(root, config)
    elif mode == "sequence":
        report = _sequence(root, config["plan"])
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    sys.stdout.write(json.dumps(report))


if __name__ == "__main__":
    main(sys.argv)
