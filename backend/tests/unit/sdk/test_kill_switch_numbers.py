"""Each number on the Turning a Flag Off page is read from its SDK's own source (#1519).

``docs/feature-flags/kill-switch.md`` says how long a flag turned off takes to
reach each client. Every number on it is a unit of its claims registry, and the
test holding the unit reads the number from the SDK's source, never from the
page. A default that moves fails here until the expected value below, and the
page with it, move too:

* ``CACHE_DEFAULTS`` has one entry per directory under ``sdk/``: the source
  file, an anchored regular expression whose group ``value`` is the number, and
  the number as the page states it, in that SDK's own unit. Each expression
  must match exactly once: no match is a renamed or moved constant, and two is
  an expression that no longer says which one it reads.
* ``USES`` holds the lines where a client takes a constant as its default
  (``config.cacheTtlMs ?? DEFAULT_CACHE_TTL_MS``) and where the edge adapters
  pass the caller's KV lifetime on, so a default written in place of the
  constant fails too.
* The directories under ``sdk/`` are exactly the table's keys plus
  ``NO_CONSTANT_OF_ITS_OWN``, which is empty: both OpenFeature providers carry
  their own 300-second default, so each is a row. A new SDK fails here until
  the page has a row for it.
* ``CLEAR`` is the method the page names for clearing each SDK's cache, found
  the same way; the Go SDK has none, and its absence is checked.
* The headline figures are derived from the rows, not typed: every SDK but the
  edge SDK keeps an answer 5 minutes, the edge SDK 1. With a KV store the edge
  figure is the in-memory lifetime plus the KV entry's lifetime, both read from
  the source; the refill that gives a KV hit a fresh in-memory lifetime is
  pinned too, since a refill with the remaining lifetime would halve it.
* ``LOCAL`` holds the local-evaluation timings of the two SDKs that evaluate
  locally: the refresh interval, its minimum and jitter, the backoff cap, and
  the wait after a refusal.

Plain file reads only: ``scripts/core_build.sh``'s copy has no ``.git``. The
file reads no page and matches no ``DOCS_READER`` pattern, so the docs-only gate
does not class it as a reader of documentation, and a claim may name its tests
with ``test:``.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK = REPO_ROOT / "sdk"

#: Seconds in one unit of a number as the source writes it.
MS = Fraction(1, 1000)
SECONDS = Fraction(1)
MINUTES = Fraction(60)


@dataclass(frozen=True)
class Pin:
    """A number in an SDK's source, and the number the page states for it."""

    path: str  # under sdk/
    pattern: str  # re.MULTILINE, anchored; group "value" is the number
    stated: int  # as the page states it, in ``unit``
    unit: Fraction = SECONDS


def matches(path: str, pattern: str) -> List[re.Match]:
    text = (SDK / path).read_text(encoding="utf-8")
    return list(re.finditer(pattern, text, re.MULTILINE))


def read_once(path: str, pattern: str) -> re.Match:
    found = matches(path, pattern)
    assert len(found) == 1, (
        f"sdk/{path}: {pattern!r} matched {len(found)} times, not once: the"
        " default moved or the expression no longer says which one it reads"
    )
    return found[0]


def number(match: re.Match, group: str = "value") -> Fraction:
    return Fraction(match.group(group).replace("_", ""))


def seconds(pin: Pin) -> Fraction:
    return number(read_once(pin.path, pin.pattern)) * pin.unit


#: The cache lifetime default of each SDK, as the page's table states it.
CACHE_DEFAULTS: Dict[str, Tuple[Pin, ...]] = {
    "js": (
        Pin(
            "js/src/client.ts",
            r"^const DEFAULT_CACHE_TTL_MS = (?P<value>[0-9_]+);$",
            300000,
            MS,
        ),
    ),
    "react": (
        Pin(
            "react/src/client/ExperimentationClient.ts",
            r"^ +const ttl = config\.cacheTtlMs \?\? (?P<value>[0-9_]+);$",
            300000,
            MS,
        ),
    ),
    "react-native": (
        Pin(
            "react-native/src/client.ts",
            r"^const DEFAULT_CACHE_TTL_MS = (?P<value>[0-9_]+);",
            300000,
            MS,
        ),
    ),
    "python": (
        Pin(
            "python/controlvar/client.py",
            r"^ +cache_ttl_seconds: float = (?P<value>[0-9_.]+),$",
            300,
        ),
    ),
    "go": (
        Pin(
            "go/config.go",
            r"^\t+CacheTTL: +(?P<value>[0-9]+) \* time\.Minute,$",
            5,
            MINUTES,
        ),
    ),
    "java": (
        Pin(
            "java/core/src/main/java/com/controlvar/sdk/config/SdkConfig.java",
            r"^ +private long cacheTtlMs = (?P<value>[0-9_]+)L;",
            300000,
            MS,
        ),
        Pin(
            "java/spring-boot-starter/src/main/java/com/controlvar/sdk/spring/ExperimentationProperties.java",
            r"^ +private int cacheTtlSeconds = (?P<value>[0-9]+);$",
            300,
        ),
    ),
    "dotnet": (
        Pin(
            "dotnet/src/Controlvar/SdkConfig.cs",
            r"^ +public int CacheTtlSeconds \{ get; init; \} = (?P<value>[0-9]+);$",
            300,
        ),
    ),
    "ruby": (
        Pin(
            "ruby/lib/controlvar/config.rb",
            r"^ +def initialize\(base_url: nil, api_key: nil, cache_ttl: (?P<value>[0-9]+),",
            300,
        ),
    ),
    "php": (
        Pin(
            "php/src/SdkConfig.php",
            r"^ +public readonly int \$cacheTtl = (?P<value>[0-9]+),$",
            300,
        ),
    ),
    "elixir": (
        Pin(
            "elixir/lib/controlvar/config.ex",
            r"^ +cache_ttl: (?P<value>[0-9_]+),$",
            300,
        ),
    ),
    "ios": (
        Pin(
            "ios/Sources/Controlvar/Models.swift",
            r"^ +cacheTTL: TimeInterval = (?P<value>[0-9]+),\n +enableOfflineFallback: Bool = true$",
            300,
        ),
        # The deprecated initializer, which keeps a default of its own.
        Pin(
            "ios/Sources/Controlvar/Models.swift",
            r"^ +cacheTTL: TimeInterval = (?P<value>[0-9]+),\n +enableLocalEval: Bool$",
            300,
        ),
    ),
    "android": (
        Pin(
            "android/sdk/src/main/kotlin/com/controlvar/android/Models.kt",
            r"^ +val cacheTtlMs: Long = (?P<value>[0-9_]+)L,",
            300000,
            MS,
        ),
    ),
    "flutter": (
        Pin(
            "flutter/lib/src/models.dart",
            r"^ +this\.cacheTtl = const Duration\(minutes: (?P<value>[0-9]+)\),$",
            5,
            MINUTES,
        ),
    ),
    "edge": (
        Pin(
            "edge/src/client.ts",
            r"^const DEFAULT_CACHE_TTL_MS = (?P<value>[0-9_]+);",
            60000,
            MS,
        ),
    ),
    "openfeature": (
        Pin(
            "openfeature/src/ExperimentationProvider.ts",
            r"^const DEFAULT_CACHE_TTL_MS = (?P<value>[0-9_]+);$",
            300000,
            MS,
        ),
    ),
    "openfeature-python": (
        Pin(
            "openfeature-python/src/controlvar_openfeature/provider.py",
            r"^ +cache_ttl: float = (?P<value>[0-9_.]+),$",
            300,
        ),
        Pin(
            "openfeature-python/src/controlvar_openfeature/types.py",
            r"^ +cache_ttl: float = (?P<value>[0-9_.]+)$",
            300,
        ),
    ),
}

#: SDK directories with no cache lifetime of their own (PD-10: empty, or such an
#: SDK only). Both OpenFeature providers have their own, so this is empty.
NO_CONSTANT_OF_ITS_OWN: Tuple[str, ...] = ()

_KT = "android/sdk/src/main/kotlin/com/controlvar/android/ExperimentationClient.kt"
_JAVA = "java/core/src/main/java/com/controlvar/sdk/ExperimentationClient.java"

#: The methods the page names for clearing each SDK's cache: (file, definition).
#: An empty entry is an SDK the page names none for.
CLEAR: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "js": (("js/src/client.ts", r"^  clearCache\(\): void \{$"),),
    "react": (
        ("react/src/client/ExperimentationClient.ts", r"^  clearCache\(\): void \{$"),
        (
            "react/src/context/ExperimentationProvider.tsx",
            r"^export function useExperimentation\(\): ExperimentationContextValue \{$",
        ),
        (
            "react/src/context/ExperimentationProvider.tsx",
            r"^  client: ExperimentationClient;$",
        ),
    ),
    "react-native": (
        ("react-native/src/client.ts", r"^  clearCache\(\): void \{$"),
        ("react-native/src/client.ts", r"^  async clearStorage\(\): Promise<void> \{$"),
    ),
    "python": (
        ("python/controlvar/client.py", r"^    def clear_cache\(self\) -> None:$"),
    ),
    "go": (),
    "java": (
        (_JAVA, r"^    public void clearCache\(\) \{$"),
        (_JAVA, r"^    public void invalidateCache\(String userId, String key\) \{$"),
    ),
    "dotnet": (
        (
            "dotnet/src/Controlvar/ExperimentationClient.cs",
            r"^    public void ClearCache\(\)$",
        ),
    ),
    "ruby": (("ruby/lib/controlvar/client.rb", r"^    def clear_cache$"),),
    "php": (
        (
            "php/src/ExperimentationClient.php",
            r"^    public function clearCache\(\): void$",
        ),
    ),
    "elixir": (
        (
            "elixir/lib/controlvar.ex",
            r"^  def clear_cache\(client\), do: Client\.clear_cache\(client\)$",
        ),
    ),
    "ios": (
        (
            "ios/Sources/Controlvar/ExperimentationClient.swift",
            r"^    public func clearCache\(\) \{$",
        ),
    ),
    "android": (
        (_KT, r"^    fun clearCache\(\) \{$"),
        (_KT, r"^    fun invalidateCache\(userId: String, key: String\) \{$"),
    ),
    "flutter": (("flutter/lib/src/client.dart", r"^  void clearCache\(\) \{$"),),
    "edge": (("edge/src/client.ts", r"^  clearCache\(\): void \{$"),),
    # provider.client.clearCache(): the provider's public client, a JS SDK client.
    "openfeature": (
        (
            "openfeature/src/ExperimentationProvider.ts",
            r"^  readonly client: ExperimentationClient;$",
        ),
        ("js/src/client.ts", r"^  clearCache\(\): void \{$"),
    ),
    # provider.client.clear_cache(): the provider's client property, a Python SDK client.
    "openfeature-python": (
        (
            "openfeature-python/src/controlvar_openfeature/provider.py",
            r"^    @property\n    def client\(self\) -> ExperimentationClient:$",
        ),
        ("python/controlvar/client.py", r"^    def clear_cache\(self\) -> None:$"),
    ),
}

#: Where each client takes its constant as the default, and where the edge
#: adapters hand the caller's KV lifetime on (undefined unless set, so the
#: store's default follows ``cacheTtlMs``): (id, file, line). A literal written
#: in place of the constant at any of these would leave the constant, and the
#: rows above, unchanged.
USES: Tuple[Tuple[str, str, str], ...] = (
    (
        "react-native",
        "react-native/src/client.ts",
        r"^    this\.cacheTtlMs = config\.cacheTtlMs \?\? DEFAULT_CACHE_TTL_MS;$",
    ),
    (
        "edge",
        "edge/src/client.ts",
        r"^    this\.cacheTtlMs = config\.cacheTtlMs \?\? DEFAULT_CACHE_TTL_MS;$",
    ),
    (
        "openfeature",
        "openfeature/src/ExperimentationProvider.ts",
        r"^      cacheTtlMs: options\.cacheTtlMs \?\? DEFAULT_CACHE_TTL_MS,$",
    ),
    (
        "edge-cloudflare-kv",
        "edge/src/adapters/cloudflare.ts",
        r"^      \(config\.kvNamespace \? new CloudflareKvStore\(config\.kvNamespace,"
        r" config\.kvTtlSeconds\) : undefined\);$",
    ),
    (
        "edge-deno-kv",
        "edge/src/adapters/deno.ts",
        r"^    const store = config\.store \?\? \(config\.kv \? new DenoKvStore\(config\.kv,"
        r" config\.kvTtlMs\) : undefined\);$",
    ),
)

#: What the page's headline states: every SDK but the edge SDK serves a change
#: within 5 minutes, the edge SDK within 1.
REMOTE_SECONDS = 5 * 60
EDGE_SECONDS = 60

#: The edge SDK with a KV store: "about 2" minutes, an entry "up to 60 seconds
#: old" copied into memory "for another 60".
EDGE_WITH_KV_SECONDS = 120
KV_ENTRY_SECONDS = 60

#: Local evaluation: (file, pattern, number as the page states it, unit). The
#: page says 30 seconds, at least 5, up to 5 minutes apart, every 10 minutes.
LOCAL: Dict[str, Dict[str, Pin]] = {
    "js": {
        "refresh": Pin(
            "js/src/local.ts",
            r"^export const DEFAULT_REFRESH_INTERVAL_MS = (?P<value>[0-9_]+);$",
            30,
            MS,
        ),
        "minimum": Pin(
            "js/src/local.ts",
            r"^export const MIN_REFRESH_INTERVAL_MS = (?P<value>[0-9_]+);$",
            5,
            MS,
        ),
        "backoff": Pin(
            "js/src/local.ts",
            r"^export const MAX_BACKOFF_MS = (?P<value>[0-9_]+);$",
            300,
            MS,
        ),
        "refused": Pin(
            "js/src/local.ts",
            r"^export const REFUSED_RETRY_MS = (?P<value>[0-9_]+);$",
            600,
            MS,
        ),
    },
    "python": {
        "refresh": Pin(
            "python/controlvar/local.py",
            r"^DEFAULT_REFRESH_INTERVAL_SECONDS = (?P<value>[0-9_.]+)$",
            30,
        ),
        "minimum": Pin(
            "python/controlvar/local.py",
            r"^MIN_REFRESH_INTERVAL_SECONDS = (?P<value>[0-9_.]+)$",
            5,
        ),
        "backoff": Pin(
            "python/controlvar/local.py",
            r"^MAX_BACKOFF_SECONDS = (?P<value>[0-9_.]+)$",
            300,
        ),
        "refused": Pin(
            "python/controlvar/local.py",
            r"^REFUSED_RETRY_SECONDS = (?P<value>[0-9_.]+)$",
            600,
        ),
    },
}

#: The jitter on the regular refresh, as (file, pattern with groups low and high).
JITTER: Dict[str, Tuple[str, str]] = {
    "js": (
        "js/src/local.ts",
        r"Math\.round\(next\.ms \* \((?P<low>[0-9.]+) \+ Math\.random\(\) \* (?P<span>[0-9.]+)\)\)",
    ),
    "python": (
        "python/controlvar/local.py",
        r"^ +delay \*= random\.uniform\((?P<low>[0-9.]+), (?P<high>[0-9.]+)\)$",
    ),
}

#: "within 33 seconds": the refresh interval at the top of its jitter.
LOCAL_WORST_SECONDS = 33


def sdk_directories() -> List[str]:
    return sorted(
        p.name for p in SDK.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------
def test_an_expression_that_matches_nothing_or_twice_is_refused(tmp_path, monkeypatch):
    """The exactly-once rule itself, on a planted file (the positive control
    is the same file with the constant once)."""
    monkeypatch.setattr(sys.modules[__name__], "SDK", tmp_path)
    pin = Pin(
        "x/client.ts", r"^const DEFAULT_CACHE_TTL_MS = (?P<value>[0-9_]+);$", 300000, MS
    )
    (tmp_path / "x").mkdir()
    source = tmp_path / "x" / "client.ts"

    source.write_text("const DEFAULT_CACHE_TTL_MS = 300_000;\n", encoding="utf-8")
    assert seconds(pin) == 300

    source.write_text("const DEFAULT_TTL_MS = 300_000;\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="matched 0 times"):
        seconds(pin)

    source.write_text("const DEFAULT_CACHE_TTL_MS = 300_000;\n" * 2, encoding="utf-8")
    with pytest.raises(AssertionError, match="matched 2 times"):
        seconds(pin)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
def test_the_sdk_directories_are_exactly_the_table():
    """A new SDK, or one gone, fails until the page and the table follow."""
    assert not set(NO_CONSTANT_OF_ITS_OWN) & set(CACHE_DEFAULTS)
    assert sdk_directories() == sorted([*CACHE_DEFAULTS, *NO_CONSTANT_OF_ITS_OWN])
    assert sorted(CLEAR) == sorted(CACHE_DEFAULTS)


@pytest.mark.parametrize("sdk", sorted(CACHE_DEFAULTS))
def test_each_sdk_cache_lifetime_is_the_number_the_page_states(sdk):
    for pin in CACHE_DEFAULTS[sdk]:
        found = number(read_once(pin.path, pin.pattern))
        assert found == pin.stated, (
            f"sdk/{pin.path}: the default is {found}, the page states {pin.stated}"
        )


@pytest.mark.parametrize("sdk", sorted(CLEAR))
def test_each_sdk_clears_its_cache_with_the_method_the_page_names(sdk):
    for path, pattern in CLEAR[sdk]:
        read_once(path, pattern)


@pytest.mark.parametrize(
    ("path", "pattern"), [use[1:] for use in USES], ids=[use[0] for use in USES]
)
def test_each_client_takes_the_pinned_constant_as_its_default(path, pattern):
    read_once(path, pattern)


def test_the_go_sdk_has_no_method_that_clears_its_cache():
    """The page says "none: create a new client" for Go."""
    body = read_once(
        "go/client.go", r"^type Client interface \{\n(?P<body>(?:.*\n)*?)\}$"
    ).group("body")
    methods = re.findall(r"^\t(?P<name>[A-Z][A-Za-z]*)\(", body, re.MULTILINE)
    assert "EvaluateFlag" in methods, (
        f"the scan found {methods}: it is broken, not clean"
    )
    assert [m for m in methods if re.search("Cache|Clear|Invalidate|Reset", m)] == []


def test_a_go_cache_ttl_of_zero_or_less_never_expires():
    """The page: ``WithCacheTTL`` (5m); 0 or less never expires."""
    read_once(
        "go/cache.go",
        r"^\tif c\.ttl > 0 && time\.Now\(\)\.After\(entry\.expiresAt\) \{$",
    )
    read_once(
        "go/cache.go",
        r"^// If maxSize <= 0, it defaults to 1\. If ttl <= 0, entries never expire\.$",
    )


def test_the_php_cache_lives_in_the_process():
    """The page: ``cacheTtl`` (300), and never past the PHP process. The cache
    is an array on the client, and nothing in the SDK writes anywhere that
    outlives the process."""
    read_once("php/src/Cache.php", r"^    private array \$store = \[\];$")
    read_once(
        "php/src/ExperimentationClient.php",
        r"^ +\$this->cache += new Cache\(\$config->cacheTtl, \$config->maxCacheSize\);$",
    )
    sources = sorted((SDK / "php" / "src").rglob("*.php"))
    assert len(sources) >= 5, (
        f"the scan found {len(sources)} files: it is broken, not clean"
    )
    persistent = re.compile(
        r"apcu_|\bRedis\b|Memcache|file_put_contents|\$_SESSION|shm_attach|setcookie"
    )
    assert [
        p.name for p in sources if persistent.search(p.read_text(encoding="utf-8"))
    ] == []


def test_every_sdk_but_the_edge_sdk_keeps_an_answer_five_minutes_and_the_edge_sdk_one():
    lifetimes = {
        sdk: {seconds(pin) for pin in pins} for sdk, pins in CACHE_DEFAULTS.items()
    }
    assert lifetimes.pop("edge") == {EDGE_SECONDS}
    assert {
        sdk: life for sdk, life in lifetimes.items() if life != {REMOTE_SECONDS}
    } == {}


def test_the_edge_figure_with_a_kv_store_is_its_two_lifetimes_about_two_minutes():
    """``cacheTtlMs`` plus the KV entry's lifetime, each read from the source.

    A KV hit is copied into memory with a fresh ``cacheTtlMs`` (the refill),
    every write to the store passes ``cacheTtlMs``, and each adapter's
    default KV lifetime follows it: Cloudflare in seconds, rounded up, never
    below its 60-second minimum (``kvTtlSeconds`` overrides it); Deno KV
    exactly (``kvTtlMs`` overrides it). The store's own propagation is not
    modelled and the page gives it no number.
    """
    (memory,) = CACHE_DEFAULTS["edge"]
    in_memory = seconds(memory)
    read_once("edge/src/client.ts", r"^ +this\.flagCache\.set\(cacheKey, stored\);$")
    read_once(
        "edge/src/client.ts",
        r"^ +await this\.store\.put\(key, JSON\.stringify\(value\), this\.cacheTtlMs\);$",
    )

    cloudflare = "edge/src/adapters/cloudflare.ts"
    minimum = number(
        read_once(cloudflare, r"^const KV_MIN_TTL_SECONDS = (?P<value>[0-9]+);$")
    )
    read_once(
        cloudflare,
        r"^ +const seconds = this\.ttlSeconds \?\? Math\.ceil\(ttlMs / 1000\);$",
    )
    read_once(cloudflare, r"expirationTtl: Math\.max\(KV_MIN_TTL_SECONDS, seconds\)")
    cloudflare_entry = max(minimum, Fraction(math.ceil(in_memory)))

    read_once(
        "edge/src/adapters/deno.ts",
        r"^ +await this\.kv\.set\(\[KV_NAMESPACE, key\], value, \{ expireIn: this\.ttlMs \?\? ttlMs \}\);$",
    )
    deno_entry = in_memory

    assert cloudflare_entry == deno_entry == KV_ENTRY_SECONDS
    assert (
        in_memory + cloudflare_entry == in_memory + deno_entry == EDGE_WITH_KV_SECONDS
    )


@pytest.mark.parametrize("sdk", sorted(LOCAL))
def test_local_evaluation_timings_are_the_numbers_the_page_states(sdk):
    for name, pin in LOCAL[sdk].items():
        found = seconds(pin)
        assert found == pin.stated, (
            f"{sdk} {name}: sdk/{pin.path} says {found} s, the page {pin.stated} s"
        )

    path, pattern = JITTER[sdk]
    jitter = read_once(path, pattern)
    low = number(jitter, "low")
    high = low + number(jitter, "span") if sdk == "js" else number(jitter, "high")
    assert (low, high) == (Fraction(9, 10), Fraction(11, 10)), (
        "the page says give or take 10%"
    )
    assert seconds(LOCAL[sdk]["refresh"]) * high == LOCAL_WORST_SECONDS


def test_the_mobile_sdks_serve_their_stored_answer_offline_by_default():
    """React Native and iOS persist by default; Android and Flutter keep the
    offline store in memory unless given a persistent one."""
    read_once(
        "react-native/src/client.ts",
        r"^ +this\.offlineFallback = config\.offlineFallback \?\? true;$",
    )
    read_once(
        "ios/Sources/Controlvar/Models.swift", r"^ +enableOfflineFallback: Bool = true$"
    )
    read_once(
        "ios/Sources/Controlvar/ExperimentationClient.swift",
        r"^ +self\.offlineStore = offlineStore \?\? OfflineStore\(\)$",
    )
    read_once(_KT, r"^ +private val offlineStore: OfflineStore = OfflineStore\(\)$")
    read_once(
        "android/sdk/src/main/kotlin/com/controlvar/android/OfflineStore.kt",
        r"^ +private val prefs: PrefsAdapter\? = null",
    )
    read_once("flutter/lib/src/models.dart", r"^ +this\.offlineFallback = true,$")
    read_once(
        "flutter/lib/src/client.dart",
        r"_offlineStore = offlineStore \?\? InMemoryOfflineStore\(\)",
    )
    read_once(
        "flutter/lib/src/shared_preferences_offline_store.dart",
        r"^class SharedPreferencesOfflineStore ",
    )
