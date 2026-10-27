"""StreamPulse device population.

A deterministic set of simulated mobile devices (seeded RNG) following the mix in the
StreamPulse spec (§2).  Every attribute is exactly what the app and the simulator send as
targeting ``context`` to the platform, so the flag rules seeded by
``backend/scripts/seed_streampulse.py`` (``employee equals true``, ``os_version semver_gte
17.0.0`` …) evaluate against these names:

    os            "iOS" | "Android"
    os_version    semver string, e.g. "17.4.0", "12.0.0"
    app_version   semver string: 3.1.0 (30%), 3.2.0 (50%), 3.2.1 (20%)
    region        US 45% · GB 15% · DE 12% · IN 18% · BR 10%
    tier          "premium" (30%) | "free"
    employee      bool (1%)
    device_model  marketing name, e.g. "Galaxy S10"
    device_id     the user id used for assignments and flag evaluation

Stdlib only.
"""
from __future__ import annotations

import hashlib
import itertools
import random
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Iterator, Sequence

DEFAULT_POPULATION = 5000
DEFAULT_SEED = 2026

# --------------------------------------------------------------------------------------
# Mix (spec §2)
# --------------------------------------------------------------------------------------

OS_MIX: tuple[tuple[str, float], ...] = (("iOS", 0.55), ("Android", 0.45))

# Concrete versions inside each family; the family shares are the spec's.
IOS_VERSION_FAMILIES: tuple[tuple[tuple[str, ...], float], ...] = (
    (("16.6.1", "16.7.0"), 0.20),
    (("17.0.0", "17.2.1", "17.4.0", "17.5.1"), 0.60),
    (("18.0.0", "18.1.0"), 0.20),
)
ANDROID_VERSION_FAMILIES: tuple[tuple[tuple[str, ...], float], ...] = (
    (("12.0.0", "12.1.0"), 0.25),
    (("13.0.0",), 0.35),
    (("14.0.0",), 0.40),
)
REGION_MIX: tuple[tuple[str, float], ...] = (
    ("US", 0.45), ("GB", 0.15), ("DE", 0.12), ("IN", 0.18), ("BR", 0.10),
)
APP_VERSION_MIX: tuple[tuple[str, float], ...] = (("3.1.0", 0.30), ("3.2.0", 0.50), ("3.2.1", 0.20))
PREMIUM_SHARE = 0.30
EMPLOYEE_SHARE = 0.01

IOS_MODELS: tuple[str, ...] = ("iPhone 12", "iPhone 13", "iPhone 14", "iPhone 15", "iPhone 15 Pro", "iPhone SE")
ANDROID_MODELS: tuple[str, ...] = ("Pixel 6", "Pixel 7", "Pixel 8", "Galaxy S10", "Galaxy S21", "Galaxy S23", "OnePlus 11")
# Android 12 shipped on these; keeps model/version pairs believable.
ANDROID_12_MODELS: tuple[str, ...] = ("Pixel 6", "Galaxy S10", "Galaxy S21")


# --------------------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    device_id: str
    os: str
    os_version: str
    app_version: str
    region: str
    tier: str
    employee: bool
    device_model: str

    def attributes(self) -> dict[str, Any]:
        """The targeting context sent with every evaluate / assign call."""
        return asdict(self)

    @property
    def os_major(self) -> int:
        return major_version(self.os_version)

    @property
    def is_android_12(self) -> bool:
        return self.os == "Android" and self.os_major == 12

    @property
    def label(self) -> str:
        bits = [self.device_model, f"{self.os} {self.os_version}", self.region, self.tier, f"app {self.app_version}"]
        if self.employee:
            bits.append("employee")
        return " · ".join(bits)


def major_version(version: str) -> int:
    """``"17.4.0"`` → 17; anything unparsable → 0."""
    head = str(version).strip().lstrip("vV").split(".", 1)[0]
    return int(head) if head.isdigit() else 0


def _weighted(rng: random.Random, table: Sequence[tuple[Any, float]]) -> Any:
    return rng.choices([v for v, _ in table], weights=[w for _, w in table], k=1)[0]


def random_device(rng: random.Random, device_id: str) -> Device:
    """One device drawn from the spec mix."""
    os_name = _weighted(rng, OS_MIX)
    if os_name == "iOS":
        os_version = rng.choice(_weighted(rng, IOS_VERSION_FAMILIES))
        model = rng.choice(IOS_MODELS)
    else:
        os_version = rng.choice(_weighted(rng, ANDROID_VERSION_FAMILIES))
        model = rng.choice(ANDROID_12_MODELS if major_version(os_version) == 12 else ANDROID_MODELS)
    return Device(
        device_id=device_id,
        os=os_name,
        os_version=os_version,
        app_version=_weighted(rng, APP_VERSION_MIX),
        region=_weighted(rng, REGION_MIX),
        tier="premium" if rng.random() < PREMIUM_SHARE else "free",
        employee=rng.random() < EMPLOYEE_SHARE,
        device_model=model,
    )


def device_stream(seed: int = DEFAULT_SEED) -> Iterator[Device]:
    """Every device of a seed, in order, without end: ``spd-<seed>-00000``, ``-00001``, …"""
    rng = random.Random(f"streampulse-devices:{seed}")
    for i in itertools.count():
        yield random_device(rng, f"spd-{seed}-{i:05d}")


def generate_devices(count: int = DEFAULT_POPULATION, seed: int = DEFAULT_SEED) -> list[Device]:
    """Deterministic population: the same ``(count, seed)`` always yields the same devices,
    the first ``count`` of ``device_stream(seed)``."""
    return list(itertools.islice(device_stream(seed), count))


def stable_bucket(key: str, salt: str = "") -> int:
    """Deterministic 0-99 bucket for a string (MD5, like the platform's rollout hashing)."""
    digest = hashlib.md5(f"{key}:{salt}".encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest, 16) % 100


# --------------------------------------------------------------------------------------
# Presets — the five devices the StreamPulse app's device panel offers
# (demo/streampulse/src/lib/devices.ts), id for id and attribute for attribute: flags
# and experiments are bucketed by the device id and targeted on the attributes, so the
# simulator's start-up probe shows exactly what the panel shows for each.
# --------------------------------------------------------------------------------------

PRESET_DEVICES: tuple[Device, ...] = (
    Device("sp-iphone-15-us-premium", "iOS", "17.4.0", "3.2.1", "US", "premium", False, "iPhone 15"),
    Device("sp-pixel-7-de-free", "Android", "14.0.0", "3.2.0", "DE", "free", False, "Pixel 7"),
    Device("sp-galaxy-s10-us-premium", "Android", "12.0.0", "3.1.0", "US", "premium", False, "Galaxy S10"),
    Device("sp-iphone-14-gb-free", "iOS", "16.7.0", "3.2.0", "GB", "free", False, "iPhone 14"),
    Device("sp-internal-tester", "iOS", "17.4.0", "3.2.1", "US", "premium", True, "iPhone 15"),
)


def summarize(devices: Iterable[Device]) -> dict[str, dict[str, int]]:
    """Counts by os / os major / region / tier / app_version / employee (for stats and tests)."""
    out: dict[str, dict[str, int]] = {"os": {}, "os_major": {}, "region": {}, "tier": {}, "app_version": {}, "employee": {}}

    def bump(table: str, key: Any) -> None:
        out[table][str(key)] = out[table].get(str(key), 0) + 1

    for d in devices:
        bump("os", d.os)
        bump("os_major", f"{d.os} {d.os_major}")
        bump("region", d.region)
        bump("tier", d.tier)
        bump("app_version", d.app_version)
        bump("employee", d.employee)
    return out


if __name__ == "__main__":  # pragma: no cover - quick look at the population
    import json

    population = generate_devices()
    print(json.dumps(summarize(population), indent=2, sort_keys=True))
    for device in population[:5]:
        print(device.label, device.device_id)
