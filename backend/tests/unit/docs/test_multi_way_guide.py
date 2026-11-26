"""The multi-way flags guide, and the OpenFeature page's variant line (#2012).

What the guide, ``docs/feature-flags/multi-way.md``, states that nothing else
holds:

* **The user ids.** The page evaluates three users and prints a different
  value for each. The ids were picked by running the server's hash, so this
  file runs it again: the key, the split, the off value and the ids are read
  from the page's own commands, each id's value is worked out by the server's
  ``flag_values.split_bucket`` and ``split_value`` and by the ground-truth
  copy (``ground_truth.hashing``), and both must be what the page prints. A
  changed id or weight fails here, before Doc Examples runs the page.
* **The names.** The things a split serves are values. "variant" belongs to
  experiments: the page says it only in the comparison table's experiment row
  and in the OpenFeature line, and never says "variation" or "treatment". The
  check runs against planted offenders first, so a pattern that stops matching
  fails here rather than passing the scan for ever.
* **The limits.** The numbers the page gives are the API's constants, and the
  refusal it prints is the API's own message for the split it sends.
* **The OpenFeature variant.** ``docs/sdk/openfeature.md`` says, for each
  provider, that the variant stays ``"on"`` or ``"off"`` whatever value a
  split gives; each provider's source sets the variant from ``enabled`` alone.

Reads only files; no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is in the docs-only gate's "Docs content tests" through its
directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, NamedTuple

import pytest

from backend.app.core import flag_values
from backend.tests.unit.ground_truth.library import hashing

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
GUIDE = REPO_ROOT / "docs" / "feature-flags" / "multi-way.md"
OPENFEATURE = REPO_ROOT / "docs" / "sdk" / "openfeature.md"
JS_PROVIDER = REPO_ROOT / "sdk" / "openfeature" / "src" / "ExperimentationProvider.ts"
PY_PROVIDER = (
    REPO_ROOT
    / "sdk"
    / "openfeature-python"
    / "src"
    / "controlvar_openfeature"
    / "provider.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """One line, with the markup a sentence may sit in taken out."""
    return " ".join(text.replace("`", "").replace("*", "").split())


# ---------------------------------------------------------------------------
# The page's commands
# ---------------------------------------------------------------------------
BLOCK = re.compile(
    r"^```\{\.bash exec\}\n(?P<body>.*?)\n```\n(?P<expects>(?:<!-- expect: [^\n]+ -->\n)*)",
    re.MULTILINE | re.DOTALL,
)
EXPECT = re.compile(r"^<!-- expect: (?P<text>.+) -->$", re.MULTILINE)
BODY = re.compile(r"-d '(?P<json>[^']*)'")
USER = re.compile(r"user_id=(?P<user>[A-Za-z0-9-]+)")


class Block(NamedTuple):
    body: str
    expects: List[str]

    def sent(self) -> List[dict]:
        """Each JSON body the block sends with ``-d``."""
        return [json.loads(m.group("json")) for m in BODY.finditer(self.body)]

    def users(self) -> List[str]:
        return [m.group("user") for m in USER.finditer(self.body)]


def blocks(text: str) -> List[Block]:
    return [
        Block(
            m.group("body"),
            [e.group("text") for e in EXPECT.finditer(m.group("expects"))],
        )
        for m in BLOCK.finditer(text)
    ]


def _one(found: List[Block], what: str) -> Block:
    assert len(found) == 1, f"the guide has {len(found)} blocks that {what}, not one"
    return found[0]


def _answers(block: Block) -> List[dict]:
    """The block's expected evaluation answers, in order."""
    return [json.loads(e) for e in block.expects if e.startswith('{"enabled"')]


class Example(NamedTuple):
    key: str
    off_value: str
    split: List[Dict]
    evaluated: Block
    lowered: Block
    ruled: Block
    refused: Block


def example(text: str) -> Example:
    page = blocks(text)
    created = _one(
        [b for b in page if '"value_type": "string"' in b.body], "create the flag"
    )
    (flag,) = created.sent()
    split_block = _one(
        [b for b in page if b.expects and b.expects[0].startswith('[{"value"')],
        "print the split as stored",
    )
    ((sent,),) = [[body["split"]] for body in split_block.sent()]
    assert split_block.expects == [json.dumps(sent, separators=(",", ":"))], (
        "the split the page sends is not the split it prints"
    )
    evaluated = _one(
        [b for b in page if "api-keys" in b.body and b.users()], "evaluate three users"
    )
    lowered = _one(
        [b for b in page if '"rollout_percentage": 50' in b.body], "lower the rollout"
    )
    ruled = _one([b for b in page if "targeting_rules" in b.body], "set a rule's value")
    refused = _one(
        [b for b in page if b.expects and '"loc":["body","split"]' in b.expects[0]],
        "show a refused split",
    )
    return Example(
        flag["key"], flag["off_value"], sent, evaluated, lowered, ruled, refused
    )


def _served(user: str, key: str, split: List[Dict]) -> str:
    """The value the server picks for *user*, checked against the copy."""
    server = flag_values.split_value(split, flag_values.split_bucket(user, key))
    copy = hashing.split_value(user, key, split)
    assert server == copy, (user, key, server, copy)
    return server


def test_the_three_users_get_the_three_values_the_page_prints():
    ex = example(_read(GUIDE))
    users, answers = ex.evaluated.users(), _answers(ex.evaluated)
    assert len(users) == len(answers) == 3, (users, answers)
    printed = {user: answer["value"] for user, answer in zip(users, answers)}
    worked_out = {user: _served(user, ex.key, ex.split) for user in users}
    assert printed == worked_out, (
        f"the page prints {printed}; the hash gives {worked_out} for key "
        f"{ex.key!r} and the split {ex.split}: pick the ids again"
    )
    assert sorted(printed.values()) == sorted(entry["value"] for entry in ex.split)
    assert all((a["enabled"], a["reason"]) == (True, "rollout") for a in answers)


SAYS = re.compile(r"`(?P<user>user-\d+)` gets `(?P<value>[^`]+)`")


def test_the_sentence_names_the_value_each_user_gets():
    text = _read(GUIDE)
    ex = example(text)
    said = {m.group("user"): m.group("value") for m in SAYS.finditer(text)}
    assert said == {u: _served(u, ex.key, ex.split) for u in ex.evaluated.users()}


def test_lowering_the_rollout_keeps_the_value_of_each_user_still_on():
    ex = example(_read(GUIDE))
    (body,) = ex.lowered.sent()
    percentage = body["rollout_percentage"]
    assert ex.lowered.expects[0] == str(percentage)
    users, answers = ex.lowered.users(), _answers(ex.lowered)
    assert users == ex.evaluated.users() and len(answers) == 3
    on = []
    for user, answer in zip(users, answers):
        bucket = hashing.flag_bucket(user, ex.key)
        assert flag_values.percent_bucket(f"{user}:{ex.key}") == bucket
        enabled = bucket < percentage
        value = _served(user, ex.key, ex.split) if enabled else ex.off_value
        assert (answer["enabled"], answer["value"]) == (enabled, value), user
        on.append(enabled)
    # The example shows both: someone kept, someone taken away.
    assert True in on and False in on, on


def test_the_rule_serves_its_own_value_to_the_user_it_matches():
    ex = example(_read(GUIDE))
    (body,) = ex.ruled.sent()
    rules = body["targeting_rules"]
    assert "rollout_percentage" not in rules, "the page says the rule's is 100"
    (answer,) = _answers(ex.ruled)
    assert answer == {
        "enabled": True,
        "value": rules["value"],
        "reason": "targeting_rule",
    }
    (user,) = ex.ruled.users()
    # Outside the flag's 50%: only the rule's percentage lets this user in.
    assert hashing.flag_bucket(user, ex.key) >= 50


def test_the_refusal_is_the_api_message_for_the_split_the_page_sends():
    ex = example(_read(GUIDE))
    (body,) = ex.refused.sent()
    errors = flag_values.split_errors("string", body["split"])
    assert [(where, error.message()) for where, error in errors] == [
        ((), json.loads(ex.refused.expects[0])["msg"])
    ]
    assert json.loads(ex.refused.expects[0])["loc"] == ["body", "split"]


LIMITS = {
    r"fewer than (\d+) or more than (\d+) values": (
        flag_values.MIN_SPLIT_ENTRIES,
        flag_values.MAX_SPLIT_ENTRIES,
    ),
    r"add up to exactly (\d+)": (flag_values.SPLIT_TOTAL,),
    r"whole number from 0 to (\d+)": (flag_values.SPLIT_TOTAL,),
    r"more than (\d+) bytes as compact JSON, or more than (\d+) levels deep": (
        flag_values.MAX_VALUE_BYTES,
        flag_values.MAX_VALUE_DEPTH,
    ),
    r"more than (\d+) bytes of split values": (flag_values.MAX_SERVED_VALUE_BYTES,),
    r"lists (\d+) to (\d+) values": (
        flag_values.MIN_SPLIT_ENTRIES,
        flag_values.MAX_SPLIT_ENTRIES,
    ),
}


@pytest.mark.parametrize("pattern, numbers", list(LIMITS.items()), ids=list(LIMITS))
def test_each_limit_on_the_page_is_the_apis(pattern, numbers):
    found = re.findall(pattern, _flat(_read(GUIDE)))
    assert found, f"the guide no longer says {pattern!r}"
    for match in found:
        stated = tuple(int(n) for n in ((match,) if isinstance(match, str) else match))
        assert stated == numbers, (pattern, stated, numbers)


# ---------------------------------------------------------------------------
# The names
# ---------------------------------------------------------------------------
VARIANT = re.compile(r"\bvariants?\b", re.IGNORECASE)
NEVER = re.compile(r"\b(?:variations?|treatments?)\b", re.IGNORECASE)
#: The two places the page may say "variant": the experiment row of the
#: comparison table, and the OpenFeature sentence.
ALLOWED = ("| An experiment |", "The OpenFeature providers report the variant")


def naming_problems(text: str) -> List[str]:
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if NEVER.search(line):
            problems.append(f"line {number} says {NEVER.search(line).group(0)!r}")
        if VARIANT.search(line) and not line.startswith(ALLOWED):
            problems.append(f"line {number} says 'variant' outside its two places")
    return problems


@pytest.mark.parametrize(
    "line",
    [
        "Each variant gets a share of the users.",
        "The split's variants are fixed.",
        "A variation of the flag.",
        "Users in the treatment get blue.",
        "| A flag with a split | No | Variants | Yes |",
    ],
)
def test_the_naming_check_fires_on_a_planted_offender(line):
    assert naming_problems(line), line


def test_the_naming_check_leaves_the_two_allowed_places_alone():
    allowed = [
        "| An experiment | Yes: each variant's metrics, compared with the control | No | Yes |",
        'The OpenFeature providers report the variant `"on"` for every user',
        "A split lists 3 to 10 values, each with a weight.",
    ]
    assert naming_problems("\n".join(allowed)) == []


def test_the_guide_says_variant_only_in_its_two_places():
    text = _read(GUIDE)
    assert naming_problems(text) == []
    # Both places still exist, so the allowance is not a dead letter.
    lines = text.splitlines()
    for start in ALLOWED:
        assert any(line.startswith(start) and VARIANT.search(line) for line in lines), (
            start
        )


# ---------------------------------------------------------------------------
# The OpenFeature variant
# ---------------------------------------------------------------------------
#: What docs/sdk/openfeature.md says of the variant, for each provider.
VARIANT_LINE = (
    'The variant is "on" for every user the flag is on for, whichever value a '
    'split or a rule gives them, and "off" for a user it is off for, as the '
    "table says."
)
#: Each provider's one assignment of the variant: from enabled alone.
JS_VARIANT = "variant: evaluation.enabled ? 'on' : 'off',"
PY_VARIANT = 'variant="on" if evaluation.enabled else "off"'
#: The table rows of an answer that is on or off, in both providers' sections.
VARIANT_ROWS = (
    '| reason: "targeting_rule", on / off | the on value (or the split\'s or the '
    'rule\'s own value) / the off value | TARGETING_MATCH | "on" / "off" |',
    '| reason: "rollout", on / off | the on value (or the split\'s value) / the '
    'off value | SPLIT | "on" / "off" |',
)


def _code_lines(text: str, comment: str) -> List[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith((comment, "*", "/*", '"""'))
    ]


def test_the_openfeature_page_says_the_variant_stays_on_or_off_for_each_provider():
    page = _read(OPENFEATURE)
    flat = _flat(page.replace("[split](../feature-flags/multi-way.md)", "split"))
    assert flat.count(VARIANT_LINE) == 2, (
        "openfeature.md says, once for each provider, that the variant is on or "
        "off whatever value a split gives"
    )
    rows = [_flat(line) for line in page.splitlines() if line.startswith("|")]
    for row in VARIANT_ROWS:
        assert rows.count(_flat(row)) == 2, row


def test_each_provider_sets_the_variant_from_enabled_alone():
    js = [
        line
        for line in _code_lines(_read(JS_PROVIDER), "//")
        if re.search(r"\bvariant\s*:", line)
    ]
    py = [
        line
        for line in _code_lines(_read(PY_PROVIDER), "#")
        if re.search(r"\bvariant\s*=", line)
    ]
    assert js == [JS_VARIANT], js
    assert len(py) == 1 and PY_VARIANT in py[0], py
