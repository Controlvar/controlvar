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
4. an empty ``.env`` is refused naming a required setting.

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

    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("production compose config: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
