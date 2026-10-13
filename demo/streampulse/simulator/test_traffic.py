"""Offline tests for the StreamPulse device simulator (no network: ApiClient is replaced)."""
from __future__ import annotations

import io
import json
import random
import re
import sys
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import devices  # noqa: E402
import traffic  # noqa: E402

TOL = 0.03  # ±3 percentage points

EVENT_METADATA_KEYS = {
    "screen_view": {"screen"},
    "play": {"track_id", "player"},
    "search": {"query", "engine", "results"},
    "notification_open": {"frequency"},
    "app_uninstall": set(),
    "wrapped_view": set(),
    "share": {"surface"},
    "badge_tap": set(),
    "onboarding_complete": {"steps"},
    "first_play": set(),
    "subscribe": {"plan", "modal"},
    "recs_impression": {"algorithm"},
}
EVENT_OWNER = {
    "play": traffic.PLAYER_FLAG,
    "search": traffic.AI_SEARCH_FLAG,
    "recs_impression": traffic.RECS_FLAG,
    "notification_open": traffic.PUSH_KEY,
    "app_uninstall": traffic.PUSH_KEY,
    "wrapped_view": traffic.WRAPPED_KEY,
    "share": traffic.WRAPPED_KEY,
    "badge_tap": traffic.BADGES_KEY,
    "onboarding_complete": traffic.ONBOARDING_KEY,
    "first_play": traffic.ONBOARDING_KEY,
    "subscribe": traffic.UPSELL_KEY,
}


class FakeClient:
    """Stands in for ApiClient: server-side flag rollout / rules, holdout + MEG, records calls."""

    def __init__(self, api_url: str = "", api_key: str = "", timeout: float = 5.0, *, seed: int = 1,
                 player_percentage: int = 5, holdout_percentage: int = 2, server_api_key: str = ""):
        self.api_key = api_key
        self.server_api_key = server_api_key
        self.rng = random.Random(seed)
        self.player_percentage = player_percentage
        self.holdout_percentage = holdout_percentage
        self.batches: list[list[dict]] = []
        self.error_reports: list[dict] = []
        self.assign_calls: list[tuple[str, str, dict]] = []
        self.evaluate_calls: list[tuple[str, str, dict]] = []

    def evaluate_flag(self, flag_key: str, user_id: str, context: dict) -> dict:
        self.evaluate_calls.append((flag_key, user_id, dict(context)))
        if flag_key == traffic.PLAYER_FLAG:
            if context.get("employee"):
                return {"key": flag_key, "enabled": True, "config": None, "reason": "targeting_rule"}
            on = devices.stable_bucket(user_id, flag_key) < self.player_percentage
            return {"key": flag_key, "enabled": on, "config": None, "reason": "rollout"}
        if flag_key == traffic.AI_SEARCH_FLAG:
            on = context.get("os") == "iOS" and devices.major_version(context["os_version"]) >= 17 and context.get("region") == "US" and context.get("tier") == "premium"
            return {"key": flag_key, "enabled": on, "config": None, "reason": "targeting_rule" if on else "rollout"}
        return {"key": flag_key, "enabled": True, "config": None, "reason": "rollout"}

    def assign(self, experiment_key: str, user_id: str, context: dict) -> dict:
        self.assign_calls.append((experiment_key, user_id, dict(context)))
        split = traffic.VARIANTS[experiment_key]
        variant = self.rng.choice(list(split))
        assigned, reason = True, "assigned"
        if devices.stable_bucket(user_id, "holdout") < self.holdout_percentage:
            variant, assigned, reason = traffic.CONTROL_VARIANT[experiment_key], False, "holdout"
        elif experiment_key in (traffic.WRAPPED_KEY, traffic.BADGES_KEY):
            wrapped_side = devices.stable_bucket(user_id, "meg") < 50
            if (experiment_key == traffic.WRAPPED_KEY) != wrapped_side:
                variant, assigned, reason = traffic.CONTROL_VARIANT[experiment_key], False, "mutual_exclusion"
        return {
            "experiment_key": experiment_key,
            "user_id": user_id,
            "variant_id": f"{experiment_key}:{variant}",
            "variant_name": variant,
            "is_control": variant == traffic.CONTROL_VARIANT[experiment_key],
            "configuration": split[variant],
            "assigned": assigned,
            "reason": reason,
        }

    def batch(self, events: list[dict]) -> dict:
        assert len(events) <= traffic.BATCH_LIMIT
        self.batches.append(list(events))
        return {"success_count": len(events), "failure_count": 0}

    def report_error(self, report: dict) -> dict:
        self.error_reports.append(dict(report))
        return {"id": "err", "error_type": report["error_type"]}

    def report_errors(self, reports: list[dict]) -> dict:
        assert len(reports) <= traffic.ERROR_BATCH_LIMIT
        self.error_reports.extend(dict(r) for r in reports)
        return {"success_count": len(reports), "failure_count": 0}


class FailingClient(FakeClient):
    def __init__(self, status: int, **kw):
        super().__init__(**kw)
        self.status = status

    def evaluate_flag(self, flag_key, user_id, context):
        raise traffic.ApiError(self.status, "nope", "http://x/api/v1/feature-flags/evaluate/" + flag_key)


def run_ticks(n: int, seed: int = 42, incident: str = "off", devices_n: int = 5000, **client_kw):
    rng = random.Random(seed)
    client = FakeClient(seed=seed + 1, **client_kw)
    stats = traffic.Stats(incident=incident)
    population = traffic.Population(devices.generate_devices(devices_n, devices.DEFAULT_SEED))
    plans = [traffic.run_tick(client, rng, population, stats, incident=incident) for _ in range(n)]
    return client, stats, plans


def all_events(client: FakeClient) -> list[dict]:
    return [e for batch in client.batches for e in batch]


# --------------------------------------------------------------------------------------


def test_population_is_deterministic_and_matches_the_spec_mix():
    pop = devices.generate_devices(20000, seed=2026)
    again = devices.generate_devices(20000, seed=2026)
    assert [d.device_id for d in pop[:50]] == [d.device_id for d in again[:50]]
    assert pop[0] == again[0]
    n = len(pop)
    share = lambda pred: sum(1 for d in pop if pred(d)) / n  # noqa: E731
    assert share(lambda d: d.os == "iOS") == pytest.approx(0.55, abs=TOL)
    assert share(lambda d: d.os == "Android" and d.os_major == 12) == pytest.approx(0.45 * 0.25, abs=TOL)
    assert share(lambda d: d.os == "iOS" and d.os_major == 17) == pytest.approx(0.55 * 0.60, abs=TOL)
    assert share(lambda d: d.region == "US") == pytest.approx(0.45, abs=TOL)
    assert share(lambda d: d.region == "IN") == pytest.approx(0.18, abs=TOL)
    assert share(lambda d: d.tier == "premium") == pytest.approx(0.30, abs=TOL)
    assert share(lambda d: d.employee) == pytest.approx(0.01, abs=0.01)
    assert share(lambda d: d.app_version == "3.2.1") == pytest.approx(0.20, abs=TOL)
    assert {d.os for d in pop} == {"iOS", "Android"}
    assert all(d.device_model in devices.ANDROID_12_MODELS for d in pop if d.is_android_12)
    # attributes() is exactly the context the app sends
    assert set(pop[0].attributes()) == {"device_id", "os", "os_version", "app_version", "region", "tier", "employee", "device_model"}
    tester = devices.PRESET_DEVICES[-1]
    assert tester.employee and tester.os == "iOS" and tester.app_version == "3.2.1"
    assert devices.PRESET_DEVICES[2].is_android_12


def test_primary_metric_rates_match_spec_over_6000_sessions():
    _, stats, plans = run_ticks(6000, devices_n=100000)
    assert stats.ticks == 6000
    expectations = {
        traffic.PUSH_KEY: (traffic.NOTIFICATION_OPEN_RATE, TOL),
        traffic.WRAPPED_KEY: (traffic.WRAPPED_VIEW_RATE, TOL),
        traffic.BADGES_KEY: (traffic.BADGE_TAP_RATE, TOL),
        traffic.UPSELL_KEY: (traffic.SUBSCRIBE_RATE, 0.012),
    }
    for key, (table, tol) in expectations.items():
        for variant, expected in table.items():
            n = stats.variant_devices[key][variant]
            assert n > 500, f"{key}/{variant} only got {n} devices"
            observed = stats.rate(key, variant)
            assert observed == pytest.approx(expected, abs=tol), f"{key}/{variant}: {observed:.3f} vs {expected}"
    assert stats.rate(traffic.PUSH_KEY, "three_weekly") > stats.rate(traffic.PUSH_KEY, "daily")
    # onboarding only runs in a device's first session (repeat sessions skip it)
    assert any(not p.first_session for p in plans)
    for variant, expected in traffic.ONBOARDING_COMPLETE_RATE.items():
        pool = [p for p in plans if p.first_session and p.assigned(traffic.ONBOARDING_KEY) and p.variant(traffic.ONBOARDING_KEY) == variant]
        assert len(pool) > 500
        observed = sum(1 for p in pool if p.converted(traffic.ONBOARDING_KEY)) / len(pool)
        assert observed == pytest.approx(expected, abs=TOL), f"onboarding_complete {variant}: {observed:.3f}"
    assert not any(p.converted(traffic.ONBOARDING_KEY) for p in plans if not p.first_session)
    # holdout + mutual exclusion show up as assigned:false reasons
    assert stats.unassigned[traffic.PUSH_KEY]["holdout"] > 0
    assert stats.unassigned[traffic.WRAPPED_KEY]["mutual_exclusion"] > 1000
    assert stats.unassigned[traffic.BADGES_KEY]["mutual_exclusion"] > 1000


def test_secondary_rates_match_spec():
    _, _, plans = run_ticks(6000, seed=9, devices_n=100000)
    for variant, expected in traffic.APP_UNINSTALL_RATE.items():
        pool = [p for p in plans if p.assigned(traffic.PUSH_KEY) and p.variant(traffic.PUSH_KEY) == variant]
        observed = sum(1 for p in pool if p.outcomes["app_uninstall"]) / len(pool)
        assert observed == pytest.approx(expected, abs=0.01), f"app_uninstall {variant}: {observed:.3f}"
    for variant, expected in traffic.FIRST_PLAY_RATE.items():
        pool = [p for p in plans if p.assigned(traffic.ONBOARDING_KEY) and p.variant(traffic.ONBOARDING_KEY) == variant]
        observed = sum(1 for p in pool if p.outcomes["first_play"]) / len(pool)
        assert observed == pytest.approx(expected, abs=TOL), f"first_play {variant}: {observed:.3f}"
        assert all(p.outcomes["onboarding_complete"] for p in pool if p.outcomes["first_play"])
    viewers = [p for p in plans if p.outcomes["wrapped_view"]]
    share = sum(1 for p in viewers if p.outcomes["share"]) / len(viewers)
    assert share == pytest.approx(traffic.SHARE_RATE_OF_VIEWERS["wrapped_2026"], abs=TOL)


def test_unassigned_experiments_get_no_events():
    client, _, plans = run_ticks(400, seed=5)
    unassigned = [(p, key) for p in plans for key in traffic.EXPERIMENT_KEYS if not p.assigned(key)]
    assert unassigned, "expected some holdout / mutual-exclusion devices"
    for plan, key in unassigned:
        assert not any(e.get("experiment_key") == key for e in plan.events), f"{plan.user_id} got events for unassigned {key}"
    assigned_with_events = [p for p in plans if any("experiment_key" in e for e in p.events)]
    assert assigned_with_events
    # a fully held-out device still produces flag-attributed events (home feed, player)
    held_out = [p for p in plans if all(not p.assigned(k) for k in traffic.EXPERIMENT_KEYS)]
    if held_out:
        assert all("feature_flag_key" in e for e in held_out[0].events)
        assert held_out[0].events


def test_evaluate_and_assign_calls_carry_the_device_context():
    client, _, plans = run_ticks(30, seed=2)
    assert len(client.evaluate_calls) == 30 * len(traffic.FLAG_KEYS)
    assert len(client.assign_calls) == 30 * len(traffic.EXPERIMENT_KEYS)
    device = plans[0].device
    first_evals = client.evaluate_calls[: len(traffic.FLAG_KEYS)]
    assert [c[0] for c in first_evals] == list(traffic.FLAG_KEYS)
    assert all(c[1] == device.device_id and c[2] == device.attributes() for c in first_evals)
    first_assigns = client.assign_calls[: len(traffic.EXPERIMENT_KEYS)]
    assert [c[0] for c in first_assigns] == list(traffic.EXPERIMENT_KEYS)
    assert all(c[1] == device.device_id and c[2] == device.attributes() for c in first_assigns)
    assert set(device.attributes()) == {"device_id", "os", "os_version", "app_version", "region", "tier", "employee", "device_model"}
    # the AI-search rule only turns on for iOS 17+ / US / premium
    for plan in plans:
        assert plan.flag_on(traffic.AI_SEARCH_FLAG) == traffic.is_ai_search_target(plan.device)


def test_batch_payloads_use_spec_event_names_and_keys():
    client, _, plans = run_ticks(600, seed=3, devices_n=100000)
    events = all_events(client)
    assert events
    required = {"event_type", "event_name", "user_id", "metadata", "timestamp"}
    seen: set[str] = set()
    for e in events:
        assert required <= set(e), e
        assert e["event_type"] == e["event_name"]
        assert e["event_name"] in EVENT_METADATA_KEYS, e["event_name"]
        assert set(e["metadata"]) == EVENT_METADATA_KEYS[e["event_name"]], e
        assert ("experiment_key" in e) ^ ("feature_flag_key" in e), e
        datetime.fromisoformat(e["timestamp"])
        owner = e.get("experiment_key") or e.get("feature_flag_key")
        if e["event_name"] in EVENT_OWNER:
            assert owner == EVENT_OWNER[e["event_name"]], e
        else:
            assert owner in traffic.EXPERIMENT_KEYS + traffic.FLAG_KEYS
        assert (e["event_name"] == "subscribe") == ("value" in e)
        seen.add(e["event_name"])
    assert seen == set(EVENT_METADATA_KEYS), f"missing event types: {set(EVENT_METADATA_KEYS) - seen}"
    screens = {e["metadata"]["screen"] for e in events if e["event_name"] == "screen_view"}
    assert screens == {"onboarding", "home", "player", "search", "notifications", "profile", "payments"}
    # play events say which player rendered
    for plan in plans:
        for e in plan.events:
            if e["event_name"] == "play":
                assert e["metadata"]["player"] == ("v2" if plan.flag_on(traffic.PLAYER_FLAG) else "classic")
            if e["event_name"] == "search":
                assert e["metadata"]["engine"] == ("ai" if plan.flag_on(traffic.AI_SEARCH_FLAG) else "keyword")


def test_incident_android12_crashes_only_affected_devices_with_the_flag_on():
    # No incident: never a crash report.
    client, stats, _ = run_ticks(300, seed=11, player_percentage=25)
    assert client.error_reports == [] and stats.crash_reports == 0

    client, stats, plans = run_ticks(1500, seed=11, incident="android12", player_percentage=25)
    crashed = [p for p in plans if p.crash_reports]
    assert crashed, "expected crashes with the flag at 25%"
    for plan in crashed:
        assert plan.device.is_android_12 and plan.flag_on(traffic.PLAYER_FLAG)
        assert traffic.is_affected_by_incident(plan.device)
        assert len(plan.crash_reports) == 1 + traffic.INCIDENT_RELAUNCHES
        assert plan.relaunches == traffic.INCIDENT_RELAUNCHES
        assert not any(e["event_name"] == "play" for e in plan.events), "a crashed session never plays"
        assert plan.events[-1]["event_name"] == "screen_view" and plan.events[-1]["metadata"]["screen"] == "player"
        for report in plan.crash_reports:
            assert report["feature_flag_key"] == traffic.PLAYER_FLAG
            assert report["error_type"] == "crash"
            assert report["message"] == "NullPointerException in PlayerV2Fragment"
            assert report["user_id"] == plan.user_id
            assert set(report["metadata"]) == {"os", "os_version", "device_model", "app_version"}
            assert report["metadata"]["os"] == "Android"
    for plan in plans:
        opened_player = plan.outcomes["play"] or plan.outcomes["crash"]
        should_crash = plan.device.is_android_12 and plan.flag_on(traffic.PLAYER_FLAG) and traffic.is_affected_by_incident(plan.device) and opened_player
        assert bool(plan.crash_reports) == should_crash, plan.device
    assert sum(1 for p in plans if p.outcomes["crash"]) == len(crashed) == stats.crashed_ticks
    # every report reached the errors endpoint; relaunches re-evaluated the flag
    assert len(client.error_reports) == sum(len(p.crash_reports) for p in plans) == stats.crash_reports
    relaunch_evals = [c for c in client.evaluate_calls if c[0] == traffic.PLAYER_FLAG]
    assert len(relaunch_evals) == len(plans) + sum(p.relaunches for p in plans)
    assert stats.evaluations == len(client.evaluate_calls)
    # the incident oversamples Android 12 and crosses the 5% critical error rate at 25% rollout
    android12_share = sum(1 for p in plans if p.device.is_android_12) / len(plans)
    assert android12_share > 0.4
    error_rate = stats.crash_reports / len(relaunch_evals)
    assert error_rate > 0.08, f"error rate {error_rate:.3f} would not trip the 5% threshold"
    assert "incident=android12" in stats.render()


def test_chunking_never_exceeds_batch_limit():
    chunks = list(traffic.chunked([{"i": i} for i in range(250)]))
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_dry_run_prints_plan_without_network():
    out = io.StringIO()

    def boom(*a, **k):
        raise AssertionError("dry run must not construct an API client")

    rc = traffic.main(["--dry-run", "--seed", "11"], client_factory=boom, out=out)
    assert rc == 0
    text = out.getvalue()
    assert "dry run" in text
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    events = [json.loads(ln) for ln in lines]
    assert events and events[0]["event_name"] == "screen_view"
    out2 = io.StringIO()
    traffic.main(["--dry-run", "--seed", "11"], client_factory=boom, out=out2)
    strip_ts = lambda s: re.sub(r'"timestamp": "[^"]+"', "", s)  # noqa: E731 — wall-clock timestamps differ
    assert strip_ts(out2.getvalue()) == strip_ts(text)

    # incident dry run shows a crashing Android 12 device and its crash reports
    out3 = io.StringIO()
    assert traffic.main(["--dry-run", "--seed", "4", "--incident", "android12"], client_factory=boom, out=out3) == 0
    text3 = out3.getvalue()
    assert "Android 12." in text3 and '"crash":true' in text3
    assert text3.count("POST /tracking/errors") == 1 + traffic.INCIDENT_RELAUNCHES
    assert "NullPointerException in PlayerV2Fragment" in text3


def test_help_lists_documented_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        traffic.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--api-url", "--api-key", "--server-api-key", "--rate", "--duration", "--seed", "--devices", "--incident", "--dry-run"):
        assert flag in text


@pytest.mark.parametrize("status,expected_rc,needle", [(401, 2, "API key"), (404, 3, "seed"), (0, 4, "Cannot reach")])
def test_fatal_api_errors_exit_non_zero_with_message(status, expected_rc, needle, capsys):
    out = io.StringIO()
    rc = traffic.main(
        ["--api-key", "k", "--max-ticks", "1", "--rate", "1000", "--no-probe"],
        client_factory=lambda url, key, **kw: FailingClient(status),
        sleep=lambda s: None,
        out=out,
    )
    assert rc == expected_rc
    assert needle in capsys.readouterr().err


def test_run_stops_after_max_ticks_and_reports_with_preset_probe():
    out = io.StringIO()
    made = []

    def factory(url, key, **kw):
        c = FakeClient(seed=5)
        made.append(c)
        return c

    rc = traffic.main(["--api-key", "k", "--max-ticks", "25", "--rate", "1000", "--seed", "1"], client_factory=factory, sleep=lambda s: None, out=out)
    assert rc == 0
    text = out.getvalue()
    assert "ticks=25" in text
    for key in traffic.EXPERIMENT_KEYS + traffic.FLAG_KEYS:
        assert key in text
    # the preset probe evaluated the five app presets (internal tester → targeting_rule)
    assert "Device presets" in text
    assert "employee" in text and "player_v2=ON(targeting_rule)" in text
    probe_ids = {c[1] for c in made[0].evaluate_calls if c[1].startswith("preset-")}
    assert probe_ids == {d.device_id for d in devices.PRESET_DEVICES}


def test_the_incident_needs_the_server_key(monkeypatch, capsys):
    monkeypatch.setattr(traffic, "DEFAULT_SERVER_KEY_FILE", Path("/nonexistent/.api_key_server"))
    made = []

    def factory(url, key, **kw):
        made.append(kw)
        return FakeClient(seed=5)

    rc = traffic.main(["--api-key", "k", "--incident", "android12", "--max-ticks", "1"], client_factory=factory)
    assert rc == 2
    err = capsys.readouterr().err
    assert "server key" in err and "seed_streampulse.py" in err and ".api_key_server" in err
    assert made == []

    # Without the incident nothing is reported, so no server key is needed.
    rc = traffic.main(
        ["--api-key", "k", "--max-ticks", "1", "--rate", "1000", "--no-probe"],
        client_factory=factory, sleep=lambda s: None, out=io.StringIO(),
    )
    assert rc == 0 and made == [{"server_api_key": ""}]


def test_the_server_key_is_read_from_its_file_and_passed_to_the_client(monkeypatch, tmp_path):
    server_file = tmp_path / ".api_key_server"
    server_file.write_text("srv-key\n")
    monkeypatch.setattr(traffic, "DEFAULT_SERVER_KEY_FILE", server_file)
    made = []

    def factory(url, key, **kw):
        made.append((key, kw))
        return FakeClient(seed=5)

    rc = traffic.main(
        ["--api-key", "app-key", "--incident", "android12", "--max-ticks", "1", "--rate", "1000", "--no-probe"],
        client_factory=factory, sleep=lambda s: None, out=io.StringIO(),
    )
    assert rc == 0
    assert made == [("app-key", {"server_api_key": "srv-key"})]

    made.clear()
    rc = traffic.main(
        ["--api-key", "app-key", "--server-api-key", "flag-key", "--incident", "android12", "--max-ticks", "1",
         "--rate", "1000", "--no-probe"],
        client_factory=factory, sleep=lambda s: None, out=io.StringIO(),
    )
    assert rc == 0
    assert made == [("app-key", {"server_api_key": "flag-key"})]


def test_missing_api_key_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(traffic, "DEFAULT_API_KEY_FILE", Path("/nonexistent/.api_key"))
    rc = traffic.main(["--max-ticks", "1"], client_factory=lambda *a: FakeClient())
    assert rc == 2
    assert "API key" in capsys.readouterr().err


def test_api_client_request_shape_and_error_mapping(monkeypatch):
    captured = {}

    class Resp:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data) if req.data else None
        captured["timeout"] = timeout
        return Resp(b'{"key": "streampulse_player_v2", "enabled": true, "config": null, "reason": "targeting_rule"}')

    monkeypatch.setattr(traffic.urllib.request, "urlopen", fake_urlopen)
    client = traffic.ApiClient("http://localhost:8000/", "secret", server_api_key="server-secret")
    device = devices.PRESET_DEVICES[-1]
    resp = client.evaluate_flag(traffic.PLAYER_FLAG, device.device_id, device.attributes())
    assert resp["reason"] == "targeting_rule"
    assert captured["method"] == "GET" and captured["body"] is None
    assert captured["headers"]["x-api-key"] == "secret"
    parsed = urllib.parse.urlparse(captured["url"])
    assert parsed.path == "/api/v1/feature-flags/evaluate/streampulse_player_v2"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["user_id"] == [device.device_id]
    assert json.loads(query["context"][0]) == device.attributes()
    assert "%7B" in captured["url"] and " " not in captured["url"]  # url-encoded compact JSON

    client.assign(traffic.PUSH_KEY, "u-1", {"os": "iOS"})
    assert captured["url"] == "http://localhost:8000/api/v1/tracking/assign"
    assert captured["body"] == {"experiment_key": traffic.PUSH_KEY, "user_id": "u-1", "context": {"os": "iOS"}}

    assert captured["headers"]["x-api-key"] == "secret"

    # Crash reports go out with the server key, the way the StreamPulse server forwards them.
    client.report_error({"feature_flag_key": traffic.PLAYER_FLAG, "error_type": "crash", "message": "m"})
    assert captured["url"] == "http://localhost:8000/api/v1/tracking/errors" and captured["method"] == "POST"
    assert captured["headers"]["x-api-key"] == "server-secret"
    client.report_errors([{"error_type": "crash"}])
    assert captured["url"] == "http://localhost:8000/api/v1/tracking/errors/batch"
    assert captured["body"] == {"errors": [{"error_type": "crash"}]}
    assert captured["headers"]["x-api-key"] == "server-secret"
    client.batch([{"event_type": "play"}])
    assert captured["url"] == "http://localhost:8000/api/v1/tracking/batch"
    assert captured["headers"]["x-api-key"] == "secret"

    # Without a server key a report is never sent with the app's key.
    no_server = traffic.ApiClient("http://localhost:8000/", "secret")
    with pytest.raises(ValueError, match="server key"):
        no_server.report_error({"feature_flag_key": traffic.PLAYER_FLAG, "error_type": "crash", "message": "m"})
    with pytest.raises(ValueError, match="server key"):
        no_server.report_errors([{"error_type": "crash"}])

    def raise_401(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}'))

    monkeypatch.setattr(traffic.urllib.request, "urlopen", raise_401)
    with pytest.raises(traffic.ApiError) as exc:
        client.assign(traffic.PUSH_KEY, "u-1", {})
    assert exc.value.status == 401 and "bad key" in exc.value.body

    def raise_conn(req, timeout):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(traffic.urllib.request, "urlopen", raise_conn)
    with pytest.raises(traffic.ApiError) as exc:
        client.evaluate_flag(traffic.RECS_FLAG, "u-1", {})
    assert exc.value.status == 0
    with pytest.raises(ValueError):
        client.batch([{}] * 101)
    with pytest.raises(ValueError):
        client.report_errors([{}] * 101)


def test_rate_and_duration_send_that_many_sessions_a_second_for_that_long(monkeypatch):
    """--rate 6 --duration 300: 6 sessions a second for five minutes, then it stops
    (on a clock that the paced sleeps move, so the run takes no real time)."""
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(traffic.time, "monotonic", lambda: now[0])
    made = []

    def factory(url, key, **kw):
        made.append(FakeClient(seed=3))
        return made[-1]

    out = io.StringIO()
    rc = traffic.main(
        ["--api-key", "k", "--rate", "6", "--duration", "300", "--no-probe"],
        client_factory=factory,
        sleep=sleep,
        out=out,
    )
    assert rc == 0
    assert "ticks=1800" in out.getvalue()
    assert 299.0 < now[0] <= 300.0
