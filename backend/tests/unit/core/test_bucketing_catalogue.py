"""Every MD5 in ``backend/app`` is either a bucket or not, and is listed here.

``consistent_hash.py``'s "WHERE BUCKETING HAPPENS" says which functions decide
a user's bucket, what they hash and how they read the digest. Two exact lists,
keyed by file and function, hold it to the code:

* :data:`BUCKETING`: each function that decides a bucket. The catalogue names
  it with its input and its reading, and the function gives the numbers they
  say, over ids that are not all ASCII;
* :data:`NOT_BUCKETING`: each other MD5, a cache key or the like.

An MD5 in neither list fails, and so does one that moved or went, so a new way
to bucket users is written down when it is added. The scan sees a call of
``md5`` however the name is imported and ``hashlib.new("md5")``.
"""

from __future__ import annotations

import ast
import hashlib
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, Iterator, List, Tuple

import pytest

from backend.app.core import consistent_hash
from backend.app.core.consistent_hash import bucket_of, hash_user
from backend.app.core.flag_values import percent_bucket, split_bucket
from backend.app.core.rules_engine import should_include_in_rollout
from backend.app.services.bandit_routing import pick_variant
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.global_holdout_service import holdout_bucket
from backend.app.services.llm_experiment_service import LLMExperimentService
from backend.app.services.mutual_exclusion_service import MutualExclusionService
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = pytest.mark.unit

APP = Path(consistent_hash.__file__).resolve().parents[1]
USERS = [f"user-{n}" for n in range(200)] + ["user-123", "ユーザー", "a:b", ""]
#: Ids that differ from another only in case, in spaces, in how the same letter is
#: written, or past the 255th character: a hash reads an id as it is sent.
USERS += ["User-7", "USER-7", " user-7 ", "e\u0301", "\u00e9", "a" * 300]


def _digest(text: str) -> bytes:
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).digest()


def _whole(text: str) -> int:
    """The FULL 128-bit digest, big-endian."""
    return int.from_bytes(_digest(text), "big")


def _first4(text: str) -> int:
    """The first 4 bytes, little-endian."""
    return struct.unpack_from("<I", _digest(text))[0]


# ---------------------------------------------------------------------------
# The two lists
# ---------------------------------------------------------------------------

#: ``(file under backend/app, function)``: the catalogue's name for it, and the
#: input and the reading the catalogue gives with it.
BUCKETING: Dict[Tuple[str, str], Tuple[str, str, str]] = {
    ("core/consistent_hash.py", "hash_user"): (
        "uses this module",
        'MD5("{user}:{key}")',
        "first 4 bytes LE",
    ),
    ("core/flag_values.py", "percent_bucket"): (
        "core/flag_values.percent_bucket",
        'MD5("{user}:{flag.key}")',
        "the FULL 128-bit digest, % 100",
    ),
    ("core/rules_engine.py", "should_include_in_rollout"): (
        "core/rules_engine.should_include_in_rollout",
        'MD5("{user}:{rule.id}")',
        "full digest, % 100",
    ),
    ("services/bandit_routing.py", "pick_variant"): (
        "services/bandit_routing.pick_variant",
        'MD5("{user}:{experiment.id}:bandit")',
        "full digest, % 10000",
    ),
    ("services/global_holdout_service.py", "holdout_bucket"): (
        "services/global_holdout_service.holdout_bucket",
        'MD5("{user}:{salt}")',
        "first 4 bytes LE, % 100",
    ),
    ("services/llm_experiment_service.py", "LLMExperimentService._hash_bucket"): (
        "services/llm_experiment_service.LLMExperimentService._hash_bucket",
        'MD5("{experiment_id}:{user}")',
        "full digest, % 10000",
    ),
    (
        "services/mutual_exclusion_service.py",
        "MutualExclusionService._normalized_hash",
    ): (
        "services/mutual_exclusion_service.MutualExclusionService._normalized_hash",
        'MD5("{user}:{salt}")',
        "first 4 bytes LE, / 2^32",
    ),
    (
        "services/rules_evaluation_service.py",
        "RulesEvaluationService._evaluate_percentage_bucket",
    ): (
        "services/rules_evaluation_service.RulesEvaluationService."
        "_evaluate_percentage_bucket",
        'MD5("{user}")',
        "full digest, % 100",
    ),
}

#: ``(file, function)``: what its MD5 is for. None decides a bucket.
NOT_BUCKETING: Dict[Tuple[str, str], str] = {
    ("core/evaluation_cache.py", "CacheKey._compute_hash"): "a cache key",
    ("core/rule_compiler.py", "RuleCompiler._hash_rule"): "a compiled rule's key",
    ("core/rules_engine.py", "get_stable_user_id"): "an id where a context has none",
}


def _is_md5(node: ast.Call, from_hashlib: Dict[str, str]) -> bool:
    """Whether *node* calls MD5: ``hashlib.md5``, a name imported from hashlib
    as it, or ``new("md5")``."""
    function = node.func
    if isinstance(function, ast.Attribute):
        name = function.attr
    elif isinstance(function, ast.Name):
        name = from_hashlib.get(function.id, "")
    else:
        return False
    if name == "new" and node.args:
        first = node.args[0]
        return isinstance(first, ast.Constant) and str(first.value).lower() == "md5"
    return name == "md5"


def _where(tree: ast.AST, is_it: Callable[[ast.Call], bool]) -> List[str]:
    """The function each call that *is_it* is in, as ``Class.method``."""

    def walk(node: ast.AST, inside: Tuple[str, ...]) -> Iterator[str]:
        for child in ast.iter_child_nodes(node):
            named = isinstance(
                child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            where = (*inside, child.name) if named else inside
            if isinstance(child, ast.Call) and is_it(child):
                yield ".".join(where) or "<module>"
            yield from walk(child, where)

    return sorted(walk(tree, ()))


def sites(source: str) -> List[str]:
    """The function each MD5 call of *source* is in."""
    tree = ast.parse(source)
    from_hashlib = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "hashlib"
        for alias in node.names
    }
    return _where(tree, lambda call: _is_md5(call, from_hashlib))


def test_every_md5_is_listed_and_every_listed_md5_is_there():
    each = [
        (str(path.relative_to(APP)), function)
        for path in sorted(APP.rglob("*.py"))
        for function in sites(path.read_text(encoding="utf-8"))
    ]
    found = set(each)
    assert len(each) == len(found), f"a function with two: {sorted(each)}"
    listed = {*BUCKETING, *NOT_BUCKETING}
    unlisted = sorted(f"{file}: {function}" for file, function in found - listed)
    gone = sorted(f"{file}: {function}" for file, function in listed - found)
    assert not unlisted, (
        f"an MD5 that no list names: {unlisted}. If it decides which users get "
        "what, name it in consistent_hash.py's WHERE BUCKETING HAPPENS and in "
        "BUCKETING here, with its input and its reading; if not, in NOT_BUCKETING"
    )
    assert not gone, f"a list names an MD5 that is not there: {gone}"
    assert not set(BUCKETING) & set(NOT_BUCKETING)
    assert (len(BUCKETING), len(NOT_BUCKETING)) == (8, 3)


@pytest.mark.parametrize(
    "source, functions",
    [
        ("import hashlib\ndef f(): return hashlib.md5(b'')\n", ["f"]),
        (
            "from hashlib import md5 as h\nclass C:\n    def m(self): h(b'')\n",
            ["C.m"],
        ),
        ("import hashlib\nX = hashlib.new('MD5', b'')\n", ["<module>"]),
        ("import hashlib\ndef f(): return hashlib.sha256(b'')\n", []),
        ("def md5(x): return x\ndef f(): return md5(1)\n", []),
    ],
)
def test_the_scan_sees_each_way_of_calling_it(source, functions):
    assert sites(source) == functions


def _catalogue() -> str:
    text = consistent_hash.__doc__
    _, heading, rest = text.partition("WHERE BUCKETING HAPPENS")
    body, history, _ = rest.partition("HISTORY.")
    assert heading and history
    return " ".join(body.split())


@pytest.mark.parametrize("site", sorted(BUCKETING), ids=lambda site: site[1])
def test_the_catalogue_names_each_bucketing_site_with_its_input_and_reading(site):
    name, text, reading = BUCKETING[site]
    catalogue = _catalogue()
    _, found, after = catalogue.partition(name)
    assert found, f"the catalogue does not name {name}"
    for said in (text, reading):
        assert said in after[:250], f"{name}: {said!r} is not said with it"


#: ``percent_bucket`` is one MD5 that two texts share, so a third text would be
#: a new way to bucket with no new MD5: each function that gives it one, and
#: what the catalogue calls the function and the text.
TEXTS: Dict[Tuple[str, str], Tuple[str, str]] = {
    (
        "services/feature_flag_service.py",
        "FeatureFlagService._evaluate_percentage_rollout",
    ): (
        "feature_flag_service._evaluate_percentage_rollout",
        'MD5("{user}:{flag.key}")',
    ),
    ("core/flag_values.py", "split_bucket"): (
        "flag_values.split_bucket",
        'MD5("{user}:{flag.key}:split")',
    ),
}


def calls_of(source: str, name: str) -> List[str]:
    """The function each call of *name* in *source* is in."""
    return _where(
        ast.parse(source),
        lambda call: (
            name in (getattr(call.func, "id", None), getattr(call.func, "attr", None))
        ),
    )


def test_the_texts_that_share_one_hash_are_the_ones_the_catalogue_names():
    found = {
        (str(path.relative_to(APP)), function)
        for path in sorted(APP.rglob("*.py"))
        for function in calls_of(path.read_text(encoding="utf-8"), "percent_bucket")
    }
    assert found == set(TEXTS), "a text percent_bucket is given, and no list names"
    catalogue = _catalogue()
    for function, text in TEXTS.values():
        assert f"{text} {function}" in catalogue, f"the catalogue does not say {text}"


def test_the_catalogue_names_no_site_that_is_gone():
    """The bandit's bucket moved to its own module; the catalogue named the
    route it was in."""
    catalogue = _catalogue()
    assert "endpoints/tracking.py" not in catalogue
    assert "MD5(" in catalogue


# ---------------------------------------------------------------------------
# The numbers are what the two columns say
# ---------------------------------------------------------------------------


def test_each_site_hashes_its_stated_input_and_reads_it_as_stated():
    holdout = SimpleNamespace(MAX_HASH_VALUE=2**32 - 1)
    rule = SimpleNamespace(id="r1", rollout_percentage=37)
    rollout = FeatureFlagService(db=None)
    flag = SimpleNamespace(key="my-flag", rollout_percentage=37)
    evaluator = RulesEvaluationService.__new__(RulesEvaluationService)
    weights = [(n, 1.0) for n in range(4)]
    for user in USERS:
        assert hash_user(user, "k") == _first4(f"{user}:k") / 2**32
        assert percent_bucket(user) == _whole(user) % 100
        # The two texts that share it.
        assert split_bucket(user, "my-flag") == _whole(f"{user}:my-flag:split") % 100
        assert split_bucket(user, "My-Flag") == _whole(f"{user}:My-Flag:split") % 100
        assert rollout._evaluate_percentage_rollout(flag, user) == (
            _whole(f"{user}:my-flag") % 100 < 37
        )
        if user:  # an empty id is no id: the context's own data is hashed
            assert should_include_in_rollout(rule, {"user_id": user}) == (
                _whole(f"{user}:r1") % 100 < 37
            )
        assert pick_variant(weights, "e1", user) == (
            _whole(f"{user}:e1:bandit") % 10_000 // 2500
        )
        assert holdout_bucket(user, "s") == _first4(f"{user}:s") % 100
        assert MutualExclusionService._normalized_hash(holdout, user, "s") == (
            _first4(f"{user}:s") / 2**32
        )
        assert LLMExperimentService._hash_bucket("e1", user) == (
            _whole(f"e1:{user}") % 10_000 / 10_000
        )
        assert evaluator._evaluate_percentage_bucket(user, 37) == (
            _whole(user) % 100 < 37
        )


def test_the_documented_example():
    """``user-123`` and ``my-flag``: the rollout's bucket, the split's and the
    experiment hash's are three numbers."""
    assert percent_bucket("user-123:my-flag") == 79
    assert split_bucket("user-123", "my-flag") == 49
    assert bucket_of("user-123", "my-flag") == 69
    catalogue = _catalogue()
    assert "bucket 79, where bucket_of gives 69" in catalogue
    assert "For user-123 / my-flag that is bucket 49" in catalogue
