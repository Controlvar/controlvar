#!/usr/bin/env python3
"""Check the production compose file the way compose itself reads it.

``backend/tests/unit/infrastructure/test_production_compose.py`` reads
``deploy/compose/compose.yml`` as YAML. This runs ``docker compose config``,
so the checks see real interpolation -- which is where a secret with a ``:-``
default, or a YAML error that compose reports with the same exit status as a
missing secret, would actually show.

Given a complete ``.env`` (``--env-file``), it asserts:

1. the resolved project builds nothing and runs exactly the published api and
   web images at ``--version`` plus the pinned postgres and redis;
2. only ``web`` publishes a port;
3. for each required setting, a copy of the ``.env`` with it removed, and one
   with it set empty, is refused -- and the refusal NAMES that setting (the
   exit status alone is not evidence: a YAML error also exits 15);
4. an empty ``.env`` is refused naming a required setting;
5. each optional feature key (``FEATURE_KEYS``: #1253, #256) reaches the api
   service empty from a copy of the ``.env`` that does not set it, and with
   its value from a copy that does. The shell's variables are not passed to
   compose, so the value can only have come from the file.
6. so does each optional setting in ``PASSED_SETTINGS``: FORWARDED_ALLOW_IPS,
   the proxies the API believes besides its own network.

Usage::

    python scripts/check_compose_config.py --env-file deploy/compose/.env
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yml"

REQUIRED = (
    "SECRET_KEY",
    "FIRST_SUPERUSER",
    "FIRST_SUPERUSER_PASSWORD",
    "POSTGRES_PASSWORD",
    "PUBLIC_BASE_URL",
)

#: Optional: each turns a feature on when it is set, and compose.yml passes it
#: to the api as ``${NAME:-}``. ``backend/tests/unit/infrastructure/
#: test_compose_feature_keys.py`` holds this list to the one the tests derive.
FEATURE_KEYS = (
    "WAREHOUSE_CREDENTIALS_KEYS",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)

#: Optional settings that are not feature keys, passed to the api the same
#: way, ``${NAME:-}``: empty unless ``.env`` sets them.
PASSED_SETTINGS = ("FORWARDED_ALLOW_IPS",)


def feature_key_failures(
    api_env: dict, given: dict, names: tuple = FEATURE_KEYS
) -> list[str]:
    """What is wrong with the feature keys (or *names*) on the resolved api
    service.

    *given* maps each key the ``.env`` sets to its value; a key it does not
    set must arrive as the empty string. No value is ever printed: an
    operator's ``.env`` holds real keys.
    """
    failures = []
    for name in names:
        if name not in api_env:
            failures.append(f"{name} is not passed to the api service")
        elif name in given and api_env[name] != given[name]:
            failures.append(f"{name} on the api service is not the value .env sets")
        elif name not in given and api_env[name] != "":
            failures.append(
                f"{name} on the api service is not empty, and .env does not set it"
            )
    return failures


def compose_config(env_file: Path, *args: str) -> subprocess.CompletedProcess:
    """``docker compose config`` with ONLY *env_file* -- no shell variables.

    A variable exported in the calling shell would satisfy ``${VAR:?}`` and
    turn every refusal check below into a false pass, so the environment is
    reduced to what docker itself needs.
    """
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(env_file)]
        + ["config", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def read_env(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            pairs.append((name.strip(), value))
    return pairs


def write_env(path: Path, pairs: list[tuple[str, str]]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in pairs), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--version",
        default=(ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        help="the version the image lines must carry (default: VERSION)",
    )
    args = parser.parse_args()
    failures: list[str] = []

    pairs = read_env(args.env_file)
    settings = dict(pairs)
    missing = [name for name in REQUIRED if not settings.get(name)]
    if missing:
        print(
            f"error: --env-file lacks {missing}; it must be complete", file=sys.stderr
        )
        return 2
    profile = settings.get("EXPERIMENTLY_PROFILE") or "core"

    # 1-2. The resolved project.
    result = compose_config(args.env_file, "--format", "json")
    if result.returncode != 0:
        print(f"error: docker compose config failed:\n{result.stderr}", file=sys.stderr)
        return 1
    services = json.loads(result.stdout)["services"]
    expected_images = {
        "postgres": "postgres:16-alpine",
        "redis": "redis:7-alpine",
        "api": f"ghcr.io/controlvar/controlvar:{profile}-{args.version}",
        "web": f"ghcr.io/controlvar/controlvar-web:{profile}-{args.version}",
    }
    images = {name: svc.get("image") for name, svc in services.items()}
    if images != expected_images:
        failures.append(f"images are {images}, expected {expected_images}")
    for name, svc in services.items():
        if "build" in svc:
            failures.append(f"service {name} has build: {svc['build']}")
    published = sorted(name for name, svc in services.items() if svc.get("ports"))
    if published != ["web"]:
        failures.append(f"services publishing ports: {published}, expected ['web']")
    print(f"resolved images: {sorted(images.values())}")

    # 3-4. The refusals, each naming its setting.
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.env"
        cases = []
        for name in REQUIRED:
            cases.append(
                (f"{name} unset", [(k, v) for k, v in pairs if k != name], name)
            )
            cases.append(
                (f"{name} empty", [(k, "" if k == name else v) for k, v in pairs], name)
            )
        cases.append(("an empty .env", [], None))
        for description, probe_pairs, name in cases:
            write_env(probe, probe_pairs)
            refused = compose_config(probe)
            named = [
                n
                for n in REQUIRED
                if f"required variable {n} is missing" in refused.stderr
            ]
            if refused.returncode == 0:
                failures.append(f"{description}: compose config succeeded")
            elif (name and named != [name]) or (not name and not named):
                failures.append(
                    f"{description}: refused, but not naming "
                    f"{name or 'a required setting'}: {refused.stderr.strip()}"
                )
            else:
                print(f"ok  {description}: refused naming {named[0]}")

        # 5-6. The feature keys and the passed settings: empty when the .env
        # leaves them out, and its value when it sets them.
        for label, names in (
            ("feature keys", FEATURE_KEYS),
            ("passed settings", PASSED_SETTINGS),
        ):
            base = [(k, v) for k, v in pairs if k not in names]
            markers = {
                name: f"set-in-the-env-file-{n}" for n, name in enumerate(names, 1)
            }
            for description, probe_pairs, given in (
                (f"{label} not in .env", base, {}),
                (f"{label} in .env", base + list(markers.items()), markers),
            ):
                write_env(probe, probe_pairs)
                resolved = compose_config(probe, "--format", "json")
                if resolved.returncode != 0:
                    failures.append(
                        f"{description}: docker compose config failed: "
                        f"{resolved.stderr.strip()}"
                    )
                    continue
                api = json.loads(resolved.stdout)["services"]["api"]
                problems = feature_key_failures(
                    api.get("environment") or {}, given, names
                )
                failures.extend(f"{description}: {problem}" for problem in problems)
                if not problems:
                    arrived = "with the value .env sets" if given else "empty"
                    print(f"ok  {description}: each reaches the api {arrived}")

    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("production compose config: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
