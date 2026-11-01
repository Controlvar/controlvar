"""The published local evaluators accept every ruleset the server sends (#1497, G1).

Every release in ``published-sdks.json`` (npm ``@controlvar/js-sdk`` and PyPI
``controlvar``, 0.1.1 and 0.2.0) evaluates flags locally from
``GET /api/v1/sdk/ruleset``. A document one of them cannot read is dropped
whole, and it keeps answering from its last ruleset: a later kill switch never
reaches it, and nothing on the server says so. So for each release:

* identity: the digest is checked before anything is unpacked, and every SDK
  module the harness loaded is inside that verified extraction;
* outcome: driven through a baseline, then ``ruleset-vectors.json``'s ruleset
  (the server's own output, which the unit tests keep current) plus a sentinel
  flag, then the sentinel turned off, it loads the server's ruleset and the
  sentinel answers ``{enabled: false, reason: "inactive"}`` -- the declared
  expectation ``accepted``. A refusal by format passes only if the pin says
  ``format-refused``, which is a decision made in that file;
* answers: every case is DEFER or exactly the server's ``{enabled, reason}``,
  and the cases answered locally are exactly the ``must_local`` ones.

The self-tests P1-P8 plant a defect into the inputs on every run, against the
same published artefacts, and assert the gate fails with the right text (or, for
P3's JavaScript half and P5, that it passes). Nothing here skips: no network or
no node is a failure that says so.

It needs the network (the npm and PyPI file hosts) and node. Run:
python -m pytest tests/sdk-contract -o addopts="" -q
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import logging
import shutil
import sys
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "published_sdks", Path(__file__).parent / "published_sdks.py"
)
ps = importlib.util.module_from_spec(_spec)
sys.modules["published_sdks"] = ps  # dataclasses look their module up here
_spec.loader.exec_module(ps)

#: G1 and its self-tests guard the #1497 failure; the fetcher's offline tests
#: below do not carry the mark.
regression = pytest.mark.regression

ARTEFACTS = ps.load_pins()
IDS = [f"{a.language}-{a.version}" for a in ARTEFACTS]
VECTORS = json.loads(ps.VECTORS_PATH.read_text(encoding="utf-8"))
#: The gate's verdicts. pyproject.toml's log_cli shows them live in the job
#: log, passing or failing (print output is captured and hidden on a pass).
log = logging.getLogger("published-sdk-gate")


@pytest.fixture(scope="session")
def fetched(tmp_path_factory):
    """The verified bytes of an artefact: one download per session, the digest
    checked on every call. A failure is remembered, so a missing network fails
    each test at once instead of retrying for each."""
    cache = tmp_path_factory.mktemp("published-artefacts")
    failures = {}

    def get(artefact):
        key = (artefact.url, artefact.digest)
        if key in failures:
            raise ps.GateError(failures[key])
        try:
            return ps.fetch(artefact, cache)
        except ps.GateError as exc:
            failures[key] = str(exc)
            raise

    return get


@pytest.fixture(params=ARTEFACTS, ids=IDS)
def artefact(request):
    return request.param


@pytest.fixture
def unpacked(artefact, fetched, tmp_path):
    """A fresh extraction of the verified artefact: (extraction, load root)."""
    extraction = tmp_path / "artefact"
    root = ps.extract(artefact, fetched(artefact), extraction)
    return extraction, root


def _run(artefact, root, tmp_path, vectors_path=ps.VECTORS_PATH, name="cwd"):
    return ps.run_harness(artefact, root, tmp_path / name, ps.gate_config(vectors_path))


def _planted(tmp_path, change):
    """A copy of the vectors with ``change`` applied, written beside the test."""
    vectors = json.loads(ps.VECTORS_PATH.read_text(encoding="utf-8"))
    change(vectors)
    path = tmp_path / "planted-vectors.json"
    path.write_text(json.dumps(vectors), encoding="utf-8")
    return vectors, path


def _append(flag):
    return lambda vectors: vectors["ruleset"]["flags"].append(flag)


# ---------------------------------------------------------------------------
# G1
# ---------------------------------------------------------------------------


def test_the_pins_cover_both_languages_and_declare_an_expectation():
    assert {a.language for a in ARTEFACTS} == {"js", "python"}
    assert all(a.expectation in ps.EXPECTATIONS for a in ARTEFACTS)
    assert VECTORS["counts"]["must_local"] > 0  # a corpus with none is vacuous


@regression
def test_the_published_evaluator_meets_its_expectation(artefact, unpacked, tmp_path):
    extraction, root = unpacked
    report = _run(artefact, root, tmp_path)
    log.info(ps.check_report(artefact, extraction, report, VECTORS))
    if artefact.expectation == "accepted":
        local = sum(1 for answer in report["answers"] if answer != "DEFER")
        log.info(
            "%s: %d of %d cases answered locally (counts.must_local %d)",
            artefact.label,
            local,
            len(VECTORS["cases"]),
            VECTORS["counts"]["must_local"],
        )
        assert local == VECTORS["counts"]["must_local"]


# ---------------------------------------------------------------------------
# Self-tests: each plants a defect and asserts the gate's verdict, every run
# ---------------------------------------------------------------------------


@regression
def test_p1_an_unknown_evaluation_freezes_the_evaluator(artefact, unpacked, tmp_path):
    """The failure the gate exists for: one flag the release cannot read, and
    it keeps its old ruleset, kill switch included."""
    extraction, root = unpacked
    planted = {"key": "zz-planted", "active": True, "evaluation": "local_v2"}
    vectors, path = _planted(tmp_path, _append(planted))
    report = _run(artefact, root, tmp_path, path)
    with pytest.raises(ps.GateError) as failure:
        ps.check_report(artefact, extraction, report, vectors)
    text = str(failure.value)
    assert f"{artefact.label} kept ruleset 'gate-baseline'" in text
    assert "The flag ruleset is malformed" in text
    assert "flags it cannot read: ['zz-planted']" in text
    assert 'gate-sentinel still answers {enabled: true, reason: "rollout"}' in text


@regression
def test_p2_a_rule_without_an_integer_percentage_fails(artefact, unpacked, tmp_path):
    extraction, root = unpacked
    planted = {
        "key": "zz-rule-percentage",
        "active": True,
        "evaluation": "local",
        "rollout_percentage": 0,
        "rules": [{"id": "r", "match": {"op": "and", "conditions": [], "groups": []}}],
        "default_rule": None,
    }
    vectors, path = _planted(tmp_path, _append(planted))
    report = _run(artefact, root, tmp_path, path)
    with pytest.raises(
        ps.GateError, match=r"flags it cannot read: \['zz-rule-percentage'\]"
    ):
        ps.check_report(artefact, extraction, report, vectors)


@regression
def test_p3_a_float_percentage_fails_python_and_passes_javascript(
    artefact, unpacked, tmp_path
):
    """``50.0``: JSON.parse gives 50, an integer; Python's json gives a float,
    which the Python evaluator refuses. Why both languages run."""
    extraction, root = unpacked
    planted = {
        "key": "zz-float-percentage",
        "active": True,
        "evaluation": "local",
        "rollout_percentage": 50.0,
        "rules": [],
        "default_rule": None,
    }
    vectors, path = _planted(tmp_path, _append(planted))
    assert '"rollout_percentage": 50.0' in path.read_text(encoding="utf-8")
    report = _run(artefact, root, tmp_path, path)
    if artefact.language == "python":
        with pytest.raises(
            ps.GateError, match=r"cannot read: \['zz-float-percentage'\]"
        ):
            ps.check_report(artefact, extraction, report, vectors)
    else:
        log.info(ps.check_report(artefact, extraction, report, vectors))


@regression
def test_p4_a_format_refusal_passes_only_when_declared(artefact, unpacked, tmp_path):
    extraction, root = unpacked
    vectors, path = _planted(tmp_path, lambda v: v["ruleset"].update(schema=2))
    report = _run(artefact, root, tmp_path, path)
    accepted = artefact.replace(expectation="accepted")
    with pytest.raises(ps.GateError) as failure:
        ps.check_report(accepted, extraction, report, vectors)
    assert "refused by format" in str(failure.value)
    assert '(schema 2, bucketing "md5-mod100-v1")' in str(failure.value)
    assert "but the gate expects 'accepted'" in str(failure.value)

    declared = artefact.replace(expectation="format-refused")
    log.info(ps.check_report(declared, extraction, report, vectors))
    # ...and a release declared format-refused that loads the real ruleset fails.
    real = _run(artefact, root, tmp_path, name="cwd-real")
    with pytest.raises(ps.GateError, match="expects 'format-refused', but it loaded"):
        ps.check_report(declared, extraction, real, VECTORS)


def _additive(vectors):
    """Keys the published evaluators do not know, at every level they index."""
    ruleset = vectors["ruleset"]
    ruleset["value_types"] = ["boolean", "string", "number", "json"]
    for flag in ruleset["flags"]:
        flag["value_type"] = "string"
        flag["off_value"] = "off"
        if flag["active"] and flag.get("evaluation") == "local":
            flag["on_value"] = "on"
            for rule in flag["rules"]:
                rule["value"] = "rule"
                rule["match"]["note"] = "ignored"
            if flag.get("default_rule"):
                flag["default_rule"]["value"] = "default"


@regression
def test_p5_additive_keys_pass(artefact, unpacked, tmp_path):
    extraction, root = unpacked
    vectors, path = _planted(tmp_path, _additive)
    assert '"on_value": "on"' in path.read_text(encoding="utf-8")
    report = _run(artefact, root, tmp_path, path)
    log.info(ps.check_report(artefact, extraction, report, vectors))


@regression
def test_p6_a_copy_outside_the_verified_extraction_fails_identity(
    artefact, unpacked, tmp_path
):
    """The same files, loaded from somewhere else (the repository's own build
    says the same version): the gate refuses on identity alone."""
    extraction, root = unpacked
    decoy = tmp_path / "decoy"
    shutil.copytree(extraction, decoy)
    report = _run(artefact, decoy / ps.LOAD_ROOT[artefact.language], tmp_path)
    with pytest.raises(ps.GateError) as failure:
        ps.check_report(artefact, extraction, report, VECTORS)
    assert "which is not inside the verified artefact at" in str(failure.value)
    assert str(decoy) in str(failure.value)
    # Identity is the only thing wrong with the decoy's run.
    log.info(ps.check_report(artefact, decoy, report, VECTORS))


@regression
def test_p7_a_flipped_expected_answer_fails(artefact, unpacked, tmp_path):
    extraction, root = unpacked
    case = next(c for c in VECTORS["cases"] if c["must_local"])

    def flip(vectors):
        for planted in vectors["cases"]:
            if planted["id"] == case["id"]:
                planted["expected"]["enabled"] = not planted["expected"]["enabled"]

    vectors, path = _planted(tmp_path, flip)
    report = _run(artefact, root, tmp_path, path)
    with pytest.raises(ps.GateError) as failure:
        ps.check_report(artefact, extraction, report, vectors)
    assert f"answered {case['id']} locally as" in str(failure.value)
    assert "the server says {enabled: " in str(failure.value)


def _flip(digest):
    head, body = digest[:7], digest[7:]
    if digest.startswith("sha256:"):
        return head + ("0" if body[0] != "0" else "1") + body[1:]
    raw = bytearray(base64.b64decode(body))
    raw[0] ^= 1
    return head + base64.b64encode(bytes(raw)).decode()


@regression
def test_p8_a_wrong_digest_fails_before_anything_is_unpacked(
    artefact, fetched, tmp_path
):
    data = fetched(artefact)
    wrong = artefact.replace(digest=_flip(artefact.digest))
    assert wrong.digest != artefact.digest
    destination = tmp_path / "artefact"
    with pytest.raises(ps.GateError, match="does not match the pinned digest"):
        ps.extract(wrong, data, destination)
    assert not destination.exists()
    with pytest.raises(ps.GateError, match="Refusing to unpack or run it"):
        fetched(wrong)


# ---------------------------------------------------------------------------
# The fetcher and the pins file, offline
# ---------------------------------------------------------------------------


def _pins(tmp_path, change):
    data = json.loads(ps.PINS_PATH.read_text(encoding="utf-8"))
    change(data["artefacts"])
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda a: a.append(dict(a[0])), "is pinned twice"),
        (lambda a: a[0].update(expectation="skip"), "is not one of"),
        (lambda a: a[0].update(digest="md5-abc"), "is neither"),
        (lambda a: a[0].update(url="https://example.com/x.tgz"), "is not on"),
        (lambda a: a[0].pop("expectation"), "the fields must be exactly"),
        (lambda a: a[2].update(registry="npm"), "with registry"),
        (lambda a: a.clear(), "pins nothing"),
    ],
    ids=["duplicate", "expectation", "digest", "url", "field", "registry", "empty"],
)
def test_a_malformed_pins_file_is_refused(tmp_path, change, message):
    with pytest.raises(ps.GateError, match=message):
        ps.load_pins(_pins(tmp_path, change))


def _tarball(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            content = b"x"
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, "package/dist/local.js"
            info.size = len(content) if kind == "file" else 0
            archive.addfile(info, io.BytesIO(content) if kind == "file" else None)
    return buffer.getvalue()


JS_REQUIRED = [(name, "file") for name in ps.REQUIRED["js"]]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("../escape.js", "file"), "refusing a member at '../escape.js'"),
        (("/abs/x.js", "file"), "refusing a member at '/abs/x.js'"),
        (("package/dist/link.js", "symlink"), "refusing the link"),
        (("package/dist/hard.js", "hardlink"), "refusing the link"),
    ],
    ids=["dotdot", "absolute", "symlink", "hardlink"],
)
def test_an_unsafe_tarball_member_refuses_the_whole_archive(tmp_path, extra, message):
    data = _tarball([*JS_REQUIRED, extra])
    artefact = ARTEFACTS[0].replace(digest=ps.digest_of(data, ARTEFACTS[0].digest))
    destination = tmp_path / "artefact"
    with pytest.raises(ps.GateError, match=message):
        ps.extract(artefact, data, destination)
    assert not destination.exists()


def test_a_wheel_with_a_dotdot_or_link_member_is_refused(tmp_path):
    python = next(a for a in ARTEFACTS if a.language == "python")
    for name, mode in (("../evil.py", 0o100644), ("controlvar/x.py", 0o120777)):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for required in ps.REQUIRED["python"]:
                archive.writestr(required, "")
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, "x")
        data = buffer.getvalue()
        artefact = python.replace(digest=ps.digest_of(data, python.digest))
        with pytest.raises(ps.GateError, match="refusing"):
            ps.extract(artefact, data, tmp_path / name.replace("/", "_"))


def test_only_the_wanted_members_are_written(tmp_path):
    data = _tarball(
        [*JS_REQUIRED, ("package/README.md", "file"), ("package/dist/x.d.ts", "file")]
    )
    artefact = ARTEFACTS[0].replace(digest=ps.digest_of(data, ARTEFACTS[0].digest))
    ps.extract(artefact, data, tmp_path / "artefact")
    written = sorted(
        p.relative_to(tmp_path / "artefact").as_posix()
        for p in (tmp_path / "artefact").rglob("*")
        if p.is_file()
    )
    assert written == sorted(ps.REQUIRED["js"])


def test_a_missing_network_fails_after_the_bounded_retries():
    calls, pauses = [], []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.URLError("unreachable")

    with pytest.raises(ps.GateError) as failure:
        ps.download("https://example.invalid/x.tgz", opener=opener, sleep=pauses.append)
    assert len(calls) == ps.ATTEMPTS == 3
    assert pauses == [5.0, 5.0] == [ps.PAUSE_SECONDS] * (ps.ATTEMPTS - 1)
    assert "after 3 attempts" in str(failure.value)
    assert "it does not skip" in str(failure.value)


def test_a_transient_failure_is_retried():
    attempts = []

    class Body(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise urllib.error.URLError("reset")
        return Body(b"payload")

    data = ps.download("https://example.invalid/x", opener=opener, sleep=lambda s: None)
    assert data == b"payload"
    assert len(attempts) == 2


def test_a_missing_node_is_a_named_failure(monkeypatch):
    monkeypatch.setattr(ps.shutil, "which", lambda name: None)
    with pytest.raises(ps.GateError, match="node is not on PATH.*it does not skip"):
        ps.node_binary()
