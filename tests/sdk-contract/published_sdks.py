"""The published SDK releases that evaluate flags locally, and the gate over them.

``published-sdks.json`` pins every published release with a local evaluator by
URL and digest (#1497). Three things read it through this module:

* ``test_published_ruleset.py`` (G1): each release is fed the ruleset the server
  sends (``ruleset-vectors.json``, which the unit tests keep equal to the
  server's output) and must meet its declared expectation;
* ``backend/tests/integration/api/test_ruleset_published_sdk_compat.py`` (G2):
  the Python releases are fed ``GET /api/v1/sdk/ruleset``'s own bytes;
* ``published_versions_check.py``: every release on the registries with a
  local evaluator is pinned here.

Standard library only: the SDK Contract Tests job installs pytest and nothing
else.

Getting an artefact, and what is refused:

* ``download``: at most ``ATTEMPTS`` tries, ``PAUSE_SECONDS`` apart. After the
  last one it raises ``GateError`` naming the URL. Nothing here skips: a gate
  that cannot reach the registry is red, never green.
* ``fetch``: the bytes, downloaded once per cache directory. The digest is
  checked on every call, whether the bytes came from the cache or not.
* ``extract``: checks the digest again, then refuses the whole archive if any
  member has an absolute path, a ``..`` part or a link type, then writes only
  the members the evaluator needs (``WANTED``) into a directory that must not
  exist yet.

Running one: ``run_harness`` starts ``harness/published_js_ruleset.cjs`` under
node, with the package required by absolute path, or
``harness/published_python_ruleset.py`` under ``python -I -S`` with only the
extraction on ``sys.path``. Each runs in its own process from an empty working
directory, so a published ``controlvar`` is never imported into the test
process. ``check_report`` then judges what the harness printed.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
PINS_PATH = HERE / "published-sdks.json"
VECTORS_PATH = HERE / "ruleset-vectors.json"
HARNESS_DIR = HERE / "harness"
HARNESSES = {
    "js": HARNESS_DIR / "published_js_ruleset.cjs",
    "python": HARNESS_DIR / "published_python_ruleset.py",
}

ATTEMPTS = 3
PAUSE_SECONDS = 5.0
TIMEOUT_SECONDS = 60.0
HARNESS_TIMEOUT_SECONDS = 300.0
#: No published SDK artefact is near this; a larger answer is not one.
MAX_BYTES = 20 * 1024 * 1024
#: The oldest node the published JS SDK supports (its package.json "engines").
MIN_NODE_MAJOR = 18

EXPECTATIONS = ("accepted", "format-refused")
REGISTRY_OF = {"js": "npm", "python": "pypi"}
URL_PREFIX = {
    "npm": "https://registry.npmjs.org/",
    "pypi": "https://files.pythonhosted.org/packages/",
}
FIELDS = ("language", "registry", "package", "version", "url", "digest", "expectation")

#: The only members extracted: the evaluator's runtime code and nothing else.
WANTED = {
    "js": re.compile(r"package/(?:package\.json|dist/[A-Za-z0-9_-]{1,64}\.js)"),
    "python": re.compile(r"controlvar/[A-Za-z0-9_]{1,64}\.py"),
}
#: What an artefact with a local evaluator must contain.
REQUIRED = {
    "js": (
        "package/package.json",
        "package/dist/local.js",
        "package/dist/evaluator.js",
    ),
    "python": (
        "controlvar/__init__.py",
        "controlvar/local.py",
        "controlvar/evaluator.py",
    ),
}
#: Where, inside an extraction, a harness loads the SDK from.
LOAD_ROOT = {"js": "package", "python": "."}

#: The flag the gate turns off to prove a kill switch reaches the evaluator,
#: the version of the document it starts from, and of the one turning it off.
SENTINEL = "gate-sentinel"
BASELINE_VERSION = "gate-baseline"
OFF_VERSION = "gate-sentinel-off"
PROBE_USER = "gate-user"
SENTINEL_ON = {"enabled": True, "reason": "rollout"}
SENTINEL_OFF = {"enabled": False, "reason": "inactive"}
FORMAT_REFUSAL = "a format this SDK does not understand"


class GateError(AssertionError):
    """A named failure of the published-evaluator gate. Never a skip."""


@dataclass(frozen=True)
class Artefact:
    language: str
    registry: str
    package: str
    version: str
    url: str
    digest: str
    expectation: str

    @property
    def label(self) -> str:
        return f"{self.package} {self.version} ({self.registry})"

    @property
    def file_name(self) -> str:
        return PurePosixPath(self.url).name

    def replace(self, **changes: str) -> "Artefact":
        return dataclasses.replace(self, **changes)


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def load_pins(path: Path = PINS_PATH) -> List[Artefact]:
    """Every pinned artefact, or GateError naming what is wrong with the file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("artefacts"), list):
        raise GateError(f"{path.name}: no 'artefacts' list")
    artefacts = []
    seen = set()
    for index, entry in enumerate(data["artefacts"]):
        where = f"{path.name} artefacts[{index}]"
        if not isinstance(entry, dict) or sorted(entry) != sorted(FIELDS):
            raise GateError(f"{where}: the fields must be exactly {sorted(FIELDS)}")
        if not all(isinstance(entry[name], str) and entry[name] for name in FIELDS):
            raise GateError(f"{where}: every field is a non-empty string")
        artefact = Artefact(**entry)
        if REGISTRY_OF.get(artefact.language) != artefact.registry:
            raise GateError(
                f"{where}: language {artefact.language!r} with registry "
                f"{artefact.registry!r}; expected one of {REGISTRY_OF}"
            )
        if not artefact.url.startswith(URL_PREFIX[artefact.registry]):
            raise GateError(
                f"{where}: {artefact.url} is not on {URL_PREFIX[artefact.registry]}"
            )
        if artefact.expectation not in EXPECTATIONS:
            raise GateError(
                f"{where}: expectation {artefact.expectation!r} is not one of "
                f"{list(EXPECTATIONS)}"
            )
        _algorithm(artefact.digest, where)
        identity = (artefact.registry, artefact.package, artefact.version)
        if identity in seen:
            raise GateError(f"{where}: {artefact.label} is pinned twice")
        seen.add(identity)
        artefacts.append(artefact)
    if not artefacts:
        raise GateError(f"{path.name}: pins nothing")
    return artefacts


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def _algorithm(digest: str, where: str) -> str:
    if re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", digest):
        return "sha512"
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        return "sha256"
    raise GateError(
        f"{where}: digest {digest!r} is neither sha512-<base64> (npm's integrity) "
        "nor sha256:<hex> (PyPI's)"
    )


def digest_of(data: bytes, like: str) -> str:
    """``data``'s digest, in the same form as the pinned digest ``like``."""
    if _algorithm(like, "digest") == "sha512":
        return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify(artefact: Artefact, data: bytes) -> None:
    actual = digest_of(data, artefact.digest)
    if actual != artefact.digest:
        raise GateError(
            f"{artefact.label}: {artefact.url} does not match the pinned digest "
            f"({artefact.digest}; it is {actual}). Refusing to unpack or run it."
        )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download(
    url: str,
    *,
    attempts: int = ATTEMPTS,
    pause: float = PAUSE_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """The body at ``url``. Tries ``attempts`` times, ``pause`` seconds apart."""
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "controlvar-published-sdk-gate"}
            )
            with opener(request, timeout=TIMEOUT_SECONDS) as response:
                data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise GateError(f"{url} is larger than {MAX_BYTES} bytes")
            return data
        except GateError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
            if attempt < attempts:
                sleep(pause)
    raise GateError(
        f"could not download {url} after {attempts} attempts: {last}. This check "
        "needs the network; it does not skip."
    )


def fetch(artefact: Artefact, cache_dir: Path, **download_options: Any) -> bytes:
    """The artefact's bytes, its digest checked on this call whatever their source."""
    cached = cache_dir / artefact.registry / artefact.file_name
    if cached.is_file():
        data = cached.read_bytes()
    else:
        data = download(artefact.url, **download_options)
        verify(artefact, data)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    verify(artefact, data)
    return data


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _unsafe(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or bool(re.match(r"[A-Za-z]:", name))
    )


def member_names(language: str, data: bytes) -> List[str]:
    """The member names of an artefact, read from its index only (nothing written)."""
    if language == "js":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            return archive.getnames()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.namelist()


def _members(language: str, data: bytes, label: str) -> Dict[str, bytes]:
    """The wanted members' contents; GateError for any unsafe member of the archive."""
    wanted = WANTED[language]
    found: Dict[str, bytes] = {}
    if language == "js":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if _unsafe(member.name):
                    raise GateError(f"{label}: refusing a member at {member.name!r}")
                if member.issym() or member.islnk():
                    raise GateError(f"{label}: refusing the link {member.name!r}")
                if not (member.isfile() or member.isdir()):
                    raise GateError(
                        f"{label}: refusing the special file {member.name!r}"
                    )
                if member.isfile() and wanted.fullmatch(member.name):
                    if member.name in found:
                        raise GateError(f"{label}: {member.name!r} appears twice")
                    handle = archive.extractfile(member)
                    found[member.name] = handle.read() if handle else b""
        return found
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if _unsafe(info.filename):
                raise GateError(f"{label}: refusing a member at {info.filename!r}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise GateError(f"{label}: refusing the link {info.filename!r}")
            if not info.is_dir() and wanted.fullmatch(info.filename):
                if info.filename in found:
                    raise GateError(f"{label}: {info.filename!r} appears twice")
                found[info.filename] = archive.read(info)
    return found


def extract(artefact: Artefact, data: bytes, destination: Path) -> Path:
    """Verify, then write the wanted members into ``destination`` (which must not
    exist). Returns the directory the harness loads the SDK from."""
    verify(artefact, data)
    members = _members(artefact.language, data, artefact.label)
    missing = [name for name in REQUIRED[artefact.language] if name not in members]
    if missing:
        raise GateError(f"{artefact.label} has no local evaluator: missing {missing}")
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in sorted(members.items()):
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return destination / LOAD_ROOT[artefact.language]


# ---------------------------------------------------------------------------
# The harnesses
# ---------------------------------------------------------------------------


def node_binary() -> str:
    """node, at least MIN_NODE_MAJOR, or GateError. There is no skip without it."""
    node = shutil.which("node")
    if node is None:
        raise GateError(
            "node is not on PATH, so the published JavaScript evaluators cannot be "
            f"checked. This gate needs node {MIN_NODE_MAJOR} or later; it does not "
            "skip."
        )
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=60, check=False
    ).stdout.strip()
    match = re.fullmatch(r"v(\d+)\..*", version)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        raise GateError(
            f"{node} is {version or 'unknown'}; the gate needs node {MIN_NODE_MAJOR}+"
        )
    return node


def harness_command(
    language: str, root: Path, mode: str, config: Dict[str, Any]
) -> List[str]:
    argument = json.dumps(config)
    if language == "js":
        return [node_binary(), str(HARNESSES["js"]), str(root), argument]
    # -I: no PYTHON* variables, no user site, no script directory on sys.path.
    # -S: no site-packages either, so an installed controlvar cannot be the one
    # imported. The harness puts ``root`` on sys.path and nothing else.
    return [
        sys.executable,
        "-I",
        "-S",
        str(HARNESSES["python"]),
        mode,
        str(root),
        argument,
    ]


def run_harness(
    artefact: Artefact,
    root: Path,
    workdir: Path,
    config: Dict[str, Any],
    mode: str = "vectors",
) -> Dict[str, Any]:
    """Run the artefact's harness in its own process, from the empty ``workdir``."""
    workdir.mkdir(parents=True, exist_ok=True)
    if any(workdir.iterdir()):
        raise GateError(f"the harness working directory {workdir} is not empty")
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    result = subprocess.run(
        harness_command(artefact.language, root, mode, config),
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise GateError(
            f"{artefact.label}: the harness exited {result.returncode}\n"
            f"{result.stderr[-4000:]}"
        )
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise GateError(
            f"{artefact.label}: the harness printed no report ({exc}): "
            f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
        ) from exc
    if not isinstance(report, dict):
        raise GateError(f"{artefact.label}: the harness report is not an object")
    return report


def gate_config(vectors_path: Path) -> Dict[str, Any]:
    return {
        "vectors": str(vectors_path),
        "sentinel": SENTINEL,
        "probe_user": PROBE_USER,
        "baseline_version": BASELINE_VERSION,
        "off_version": OFF_VERSION,
    }


# ---------------------------------------------------------------------------
# Judging a report
# ---------------------------------------------------------------------------


def _inside(path: str, directory: Path) -> bool:
    resolved = Path(os.path.realpath(path))
    base = Path(os.path.realpath(directory))
    return resolved == base or base in resolved.parents


LOCAL_MODULE = {"js": "local.js", "python": "local.py"}


def check_identity(artefact: Artefact, extraction: Path, report: Dict[str, Any]) -> str:
    """Every SDK module the harness loaded, the local evaluator among them, is
    inside the verified extraction, and the package says it is the pinned
    release. Returns the local evaluator's path."""
    identity = report.get("identity") or {}
    modules = list(identity.get("modules") or [])
    local = identity.get("local_module")
    if not modules or not isinstance(local, str):
        raise GateError(f"{artefact.label}: the harness reported no loaded modules")
    outside = sorted(p for p in [local, *modules] if not _inside(p, extraction))
    if outside:
        raise GateError(
            f"{artefact.label}: the harness loaded {outside[0]}, which is not inside "
            f"the verified artefact at {extraction}; its answers are not the "
            "published evaluator's"
        )
    if Path(local).name != LOCAL_MODULE[artefact.language] or local not in modules:
        raise GateError(
            f"{artefact.label}: the harness did not load the local evaluator"
        )
    if (identity.get("package"), identity.get("version")) != (
        artefact.package,
        artefact.version,
    ):
        raise GateError(
            f"{artefact.label}: the loaded package says it is "
            f"{identity.get('package')} {identity.get('version')}"
        )
    return local


def _step(report: Dict[str, Any], label: str) -> Dict[str, Any]:
    for step in report.get("steps") or []:
        if step.get("label") == label:
            return step
    raise GateError(f"the harness reported no '{label}' step")


def _shown(answer: Any) -> str:
    if answer == "DEFER":
        return "DEFER (asks the server)"
    if isinstance(answer, dict):
        return "{" + ", ".join(f"{k}: {json.dumps(v)}" for k, v in answer.items()) + "}"
    return repr(answer)


def check_outcome(
    artefact: Artefact, report: Dict[str, Any], server_version: str
) -> str:
    """The declared expectation: what the evaluator did with the server's ruleset."""
    baseline = _step(report, "baseline")
    if (
        baseline.get("version") != BASELINE_VERSION
        or baseline.get("sentinel") != SENTINEL_ON
    ):
        raise GateError(
            f"{artefact.label}: the harness's own baseline document did not load "
            f"({baseline}); the gate cannot judge this run"
        )
    server = _step(report, "server")
    off = _step(report, "sentinel-off")
    refused = report.get("refused_flags") or []
    error = server.get("error") or ""
    if artefact.expectation == "format-refused":
        if server.get("ready") or FORMAT_REFUSAL not in error:
            did = (
                f"loaded ruleset {server.get('version')!r}"
                if server.get("ready")
                else f"reported: {error or 'no error'}"
            )
            raise GateError(
                f"{artefact.label}: the gate expects 'format-refused', but it {did}"
            )
        return f"refused by format, as declared: {error}"
    if FORMAT_REFUSAL in error:
        raise GateError(
            f"{artefact.label}: refused by format ({error}) but the gate expects "
            "'accepted'. A format change is decided, not discovered: if it is "
            "meant, set this artefact's expectation to 'format-refused' in "
            "published-sdks.json."
        )
    problems = []
    if server.get("version") != server_version:
        names = f"; flags it cannot read: {refused}" if refused else ""
        problems.append(
            f"{artefact.label} kept ruleset {server.get('version')!r} instead of "
            f"loading {server_version!r}: {error or 'no error reported'}{names}"
        )
    if off.get("version") != OFF_VERSION or off.get("sentinel") != SENTINEL_OFF:
        problems.append(
            f"{SENTINEL} still answers {_shown(off.get('sentinel'))} from ruleset "
            f"{off.get('version')!r} after the kill switch (expected "
            f"{_shown(SENTINEL_OFF)} from {OFF_VERSION!r})"
        )
    if problems:
        raise GateError("\n".join(problems))
    return f"loaded ruleset {server_version}; the kill switch reached {SENTINEL}"


def check_answers(
    artefact: Artefact, report: Dict[str, Any], vectors: Dict[str, Any]
) -> int:
    """Every case DEFER or exactly the server's {enabled, reason}; the local ids
    are exactly the must_local ids. Returns the number answered locally."""
    cases = vectors["cases"]
    answers = report.get("answers")
    if not isinstance(answers, list) or len(answers) != len(cases):
        raise GateError(
            f"{artefact.label}: answered {len(answers) if isinstance(answers, list) else 'no'} "
            f"of {len(cases)} cases (the ruleset was not indexed: "
            f"{report.get('index_error') or 'no reason given'})"
        )
    wrong, local = [], set()
    for case, answer in zip(cases, answers):
        if answer == "DEFER":
            continue
        local.add(case["id"])
        expected = {k: case["expected"][k] for k in ("enabled", "reason")}
        if answer != expected:
            wrong.append(
                f"{artefact.label} answered {case['id']} locally as {_shown(answer)}; "
                f"the server says {_shown(expected)}"
            )
    must = {case["id"] for case in cases if case["must_local"]}
    problems = wrong[:10]
    if len(wrong) > 10:
        problems.append(f"... and {len(wrong) - 10} more wrong answers")
    missed, extra = sorted(must - local), sorted(local - must)
    if missed:
        problems.append(
            f"{artefact.label} sent {len(missed)} must_local cases to the server: "
            f"{missed[:5]}"
        )
    if extra:
        problems.append(
            f"{artefact.label} answered {len(extra)} cases locally that the server "
            f"answers itself: {extra[:5]}"
        )
    if len(local) != vectors["counts"]["must_local"]:
        problems.append(
            f"{artefact.label} answered {len(local)} cases locally; the vectors' "
            f"counts.must_local is {vectors['counts']['must_local']}"
        )
    if problems:
        raise GateError("\n".join(problems))
    return len(local)


def check_report(
    artefact: Artefact,
    extraction: Path,
    report: Dict[str, Any],
    vectors: Dict[str, Any],
) -> str:
    """The whole G1 judgement of one run. Returns the line the gate prints."""
    module = check_identity(artefact, extraction, report)
    outcome = check_outcome(artefact, report, vectors["ruleset"]["version"])
    if artefact.expectation == "format-refused":
        return f"{artefact.label}: OK ({module}): {outcome}"
    count = check_answers(artefact, report, vectors)
    deferred = len(vectors["cases"]) - count
    return (
        f"{artefact.label}: OK ({module}, {artefact.digest[:19]}...): {outcome}; "
        f"{count}/{vectors['counts']['must_local']} cases answered locally, every "
        f"one the server's answer; {deferred} deferred"
    )


def describe(artefacts: Sequence[Artefact]) -> List[str]:
    return [f"{a.label} {a.digest} expectation={a.expectation}" for a in artefacts]
