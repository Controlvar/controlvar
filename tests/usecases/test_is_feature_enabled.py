"""``is_feature_enabled`` / ``isFeatureEnabled`` over the wire: what this
checkout's Python and JavaScript SDKs answer, in server mode and in local
mode, against a running Controlvar (``_platform.yml``'s ``usecases-flags``
suite, nightly).

Four keys, each asked by its own user on each of the four legs (Python and
JavaScript, server and local) through the drivers' ``enabled`` command:

* a flag **on** for everyone (active, rollout 100): ``true``;
* a flag **off** for everyone (active, rollout 0): ``false``;
* an **archived** flag (created on, then archived): ``false``;
* a **missing** key: ``false``, and nothing raised: the method swallows the
  failure instead of returning a caller's default.

Each answer must equal the API's own (a raw ``GET`` of the evaluate route; the
missing key answers 404, which the method turns into ``false``), and the
``evaluate`` command of the same leg must agree with it for the three keys the
API knows. Wall clock is reported in the step summary, never gated. The signed-in
run, Node, the proxied drivers and the helpers are ``conftest.py``'s. Nothing
here skips.
"""

from __future__ import annotations

import time
from typing import Any

from usecases import admin, sdks

from conftest import Live, _created, _fail_on, _synced, proxied

#: The ruleset refresh interval of a local driver (the SDKs' minimum).
REFRESH = 5
#: What each kind of key must answer.
EXPECTED = {"on": True, "off": False, "archived": False, "missing": False}


def findings_for(
    leg: str,
    enabled: dict[str, Any],
    evaluated: dict[str, Any],
    api: dict[str, bool],
) -> list[str]:
    """The disagreements on one leg. *enabled* maps each kind to the
    ``enabled`` command's answer, *evaluated* the known kinds to the
    ``evaluate`` command's ``enabled``, *api* each kind to the API's answer."""
    found = []
    for kind, wanted in EXPECTED.items():
        if enabled.get(kind) is not wanted:
            found.append(
                f"{leg} {kind}: enabled answered {enabled.get(kind)!r}, expected {wanted!r}"
            )
        if api.get(kind) is not wanted:
            found.append(
                f"{leg} {kind}: the API says {api.get(kind)!r}, expected {wanted!r}"
            )
        if kind != "missing" and evaluated.get(kind) is not enabled.get(kind):
            found.append(
                f"{leg} {kind}: evaluate answered {evaluated.get(kind)!r}, enabled"
                f" answered {enabled.get(kind)!r}"
            )
    return found


def test_is_feature_enabled_answers_the_apis_on_every_leg(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        keys = {kind: f"uc-ife-{kind}-{live.run}" for kind in EXPECTED}
        users = {kind: f"uc-ife-{live.run}-{kind}" for kind in EXPECTED}
        _created(live.admin.create_flag(keys["on"], rollout_percentage=100), 100)
        _created(live.admin.create_flag(keys["off"], rollout_percentage=0), 0)
        archived = live.admin.create_flag(keys["archived"], rollout_percentage=100)
        (result,) = live.admin.archive(_created(archived, 100))["results"]
        if result.get("success") is not True:
            findings.append(f"archiving the flag failed: {result.get('error')!r}")

        api: dict[str, bool] = {}
        for kind in ("on", "off", "archived"):
            api[kind] = live.admin.evaluate(keys[kind], users[kind]).get("enabled")
        try:
            live.admin.evaluate(keys["missing"], users["missing"])
            findings.append("raw GET missing: answered 2xx, expected 404")
        except admin.AdminError:
            api["missing"] = False

        # Read once the flags are set: the version a local driver must hold.
        version = live.admin.ruleset()["version"]

        for sdk in sdks.SDKS:
            for mode in ("server", "local"):
                leg = f"{sdk} {mode}"
                options: dict[str, Any] = {}
                if mode == "local":
                    options["refresh_seconds"] = REFRESH
                with proxied(
                    live, sdk, node if sdk == "js" else None, mode=mode, **options
                ) as (
                    session,
                    relay,
                ):
                    if mode == "local":
                        _synced(
                            session,
                            relay,
                            version,
                            f"the {sdk} local driver (the ruleset holding the flags)",
                            caller="is_feature_enabled (setup)",
                            deadline=time.monotonic() + 3 * REFRESH,
                        )
                    enabled = {
                        kind: session.call(
                            "enabled", flag=keys[kind], user=users[kind]
                        ).get("enabled")
                        for kind in EXPECTED
                    }
                    evaluated = {
                        kind: session.call(
                            "evaluate", flag=keys[kind], user=users[kind], default=None
                        ).get("enabled")
                        for kind in EXPECTED
                        if kind != "missing"
                    }
                findings += findings_for(leg, enabled, evaluated, api)
        outcome = f"{len(EXPECTED)} keys on 4 legs, {len(findings)} findings"
    finally:
        live.summary.append(
            f"is_feature_enabled (refresh {REFRESH} s): {outcome}; {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "is_feature_enabled")


def test_the_checks_catch_a_wrong_answer() -> None:
    """The case's own checks fail on the answers they exist to catch: an archived
    flag answered on, a missing key answered on, and a disagreement between the
    two commands."""
    api = dict(EXPECTED)
    right = dict(EXPECTED)
    known = {k: v for k, v in EXPECTED.items() if k != "missing"}
    assert findings_for("leg", right, known, api) == []
    assert findings_for(
        "leg", {**right, "archived": True}, {**known, "archived": True}, api
    )
    assert findings_for("leg", {**right, "missing": True}, known, api)
    assert findings_for("leg", right, {**known, "on": False}, api)
    assert findings_for("leg", right, known, {**api, "off": True})
