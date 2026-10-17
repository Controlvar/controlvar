"""The integration pages describe the integrations API the code serves.

The GitHub and Salesforce pages, the FAQ and the endpoint list described an
integration the API does not have, and a webhook that answers differently from
what they said. On main (`modules/backend/app/api/v1/endpoints/integrations.py`,
`modules/backend/app/schemas/integration.py`):

* An integration is addressed by its **type**, in lower case:
  `/api/v1/integrations/{integration_type}` with `jira`, `salesforce` or
  `github`. There is no id in a path, and `integration_type` in a body is lower
  case too (`"GITHUB"` is a 422).
* The body field is `encrypted_config`. A `"config"` field is not an error: it is
  ignored, so an example that sent one answered 200 and changed nothing.
* Reading is ADMIN or DEVELOPER; creating, updating and deleting is ADMIN.
* A webhook answers `{"status": "received"}` for an authenticated delivery, never
  `{"processed": true}`. Every refused delivery is one 401 (a wrong or missing
  signature included); a 400 only follows a successful authentication.
* The Salesforce webhook takes a JSON object from a Flow HTTP Callout, an Apex
  callout or a relay. A native Salesforce Outbound Message sends SOAP/XML with no
  custom headers: with no header it is a 401, and with the header added by a
  proxy the XML body is a 400. So no page may say an outbound message uses or
  can set the shared-secret header.
* Nothing in the platform calls Jira, Salesforce or GitHub, and an
  authenticated delivery changes nothing: the pages said the platform synced
  both ways, pushed results to Salesforce campaigns and linked pull requests to
  experiments. `modules/backend/tests/unit/services/test_integration_wiring.py`
  fails when a call is wired, so the pages and this test change with it.

This test forbids the phrasings that were removed and requires the pages to say
what is true. It is a sweep, not a proof: a new wording of the same claim is not
caught; review is. Each forbidden pattern also has a sample it must match, so a
pattern that has silently stopped matching fails here instead of reporting
"clean" for ever. When the API changes, change the docs and this test together.

Reads only files; no git and no `modules` import, so it runs the same in
`scripts/core_build.sh`'s copy (no `.git`, no `modules/`). It names no module
path (the core/modules boundary test forbids it): the module docstrings that
describe the webhooks are swept with these same rules by
`modules/backend/tests/unit/services/test_integration_docstrings.py`. It is in
the docs-only gate's "Docs content tests" through its directory,
`backend/tests/unit/docs/`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"

#: Pages the sweep must read, named, so a wrong root fails instead of passing.
SWEEP_FLOOR = (
    "docs/api/endpoints.md",
    "docs/api/integrations.md",
    "docs/getting-started/faq.md",
    "docs/integrations/github.md",
    "docs/integrations/salesforce.md",
)

#: Where a rule applies, when it is not the whole of `docs/`.
INTEGRATION_PAGES = ("docs/integrations/", "docs/api/integrations.md")

#: (pattern, why it is wrong, the path prefixes it is limited to or None, a sample
#: of the removed text the pattern must match).
Rule = Tuple[str, str, Optional[Tuple[str, ...]], str]

STALE: Tuple[Rule, ...] = (
    (
        r"integrations/\{id\}",
        "an integration is addressed by its type, `/api/v1/integrations/{integration_type}`",
        None,
        "GET /api/v1/integrations/{id}",
    ),
    (
        r"int-uuid",
        "no route takes an id for an integration; the path parameter is the type",
        None,
        "curl -X PUT http://localhost:8000/api/v1/integrations/int-uuid-here",
    ),
    (
        r"\"processed\"\s*:\s*true",
        'a webhook answers {"status": "received"}',
        None,
        '{"processed": true}',
    ),
    (
        r"\"integration_type\"\s*:\s*\"(?:JIRA|SALESFORCE|GITHUB)\"",
        "the type is lower case (`jira`, `salesforce`, `github`); upper case is a 422",
        None,
        '"integration_type": "GITHUB",',
    ),
    (
        r"`JIRA`,\s*`SALESFORCE`,\s*`GITHUB`",
        "the `IntegrationType` values are lower case",
        None,
        "Supported `IntegrationType` values: `JIRA`, `SALESFORCE`, `GITHUB`.",
    ),
    (
        r"\"config\"\s*:",
        "the body field is `encrypted_config`; `config` is ignored, so the request does nothing",
        INTEGRATION_PAGES,
        '    "config": {',
    ),
    (
        r"/api/v1/integrations[^\n]*\((?:DEVELOPER|ANALYST)\+\)",
        "reading is ADMIN or DEVELOPER; creating, updating and deleting is ADMIN",
        None,
        "POST   /api/v1/integrations               - Create integration (DEVELOPER+)",
    ),
    (
        r"Create and manage experiments, feature flags, and integrations",
        "a DEVELOPER can read integrations but not change them",
        None,
        "| **DEVELOPER** | Create and manage experiments, feature flags, and integrations |",
    ),
    (
        r"Bad HMAC Signature|Missing Signature",
        "every refused webhook delivery is one 401; there is no separate 400 for a bad signature",
        ("docs/integrations/",),
        "### 400 Bad Request — Bad HMAC Signature",
    ),
    (
        r"Delete and recreate the integration with a consistent secret",
        "set the secret again with a PUT (the recipe in docs/api/integrations.md)",
        ("docs/integrations/",),
        "Delete and recreate the integration with a consistent secret.",
    ),
    (
        r"(?i)\boutbound messages?\b[^.\n]{0,100}\b(?:cannot|can't)\s+(?:sign|compute|hmac)",
        "a native Outbound Message is SOAP/XML with no custom headers and can use neither form; a Flow HTTP Callout, an Apex callout or a relay does",
        None,
        "A Salesforce outbound message cannot compute an HMAC over the body it sends, so it uses the header.",
    ),
    (
        r"(?i)\boutbound messages?\b[^.\n]{0,100}\b(?:uses|sends|can set)\b[^.\n]{0,40}(?:shared-secret header|X-Experimently-Webhook-Secret)",
        "a native Outbound Message cannot set the shared-secret header",
        None,
        "A Salesforce outbound message uses the shared-secret header.",
    ),
    (
        r"(?i)all an outbound message can send",
        "a native Outbound Message sends no custom header",
        None,
        "the secret in ``X-Experimently-Webhook-Secret``, which is all an outbound message can send, or",
    ),
    (
        r"(?i)header on the Salesforce outbound message",
        "the header is set by the Flow HTTP Callout, the Apex callout or the relay",
        None,
        "and the same header on the Salesforce outbound message or callout.",
    ),
    (
        r"(?i)configure a Salesforce Outbound Message",
        "a native Outbound Message cannot call the endpoint; configure a Flow HTTP Callout, an Apex callout or a relay",
        None,
        "configure a Salesforce Outbound Message (or Process Builder / Flow) to POST to:",
    ),
    (
        r"(?i)\breceives?\s+Salesforce outbound messages?",
        "the route receives JSON from a callout or a relay, not an Outbound Message",
        None,
        "Receive Salesforce outbound messages via webhook",
    ),
    (
        r"Salesforce Outbound Message matches|Monitoring \u2192 Outbound Messages",
        "the sender is a Flow HTTP Callout, an Apex callout or a relay; check its response and the debug log",
        None,
        "Confirm the **Endpoint URL** in the Salesforce Outbound Message matches your integration webhook URL",
    ),
    (
        r"(?i)\bbidirectional (?:sync|integrations?)\b",
        "nothing in the platform calls Jira, Salesforce or GitHub; only the inbound webhooks are wired",
        ("docs/",),
        "The platform supports bidirectional sync with Jira, Salesforce, and GitHub",
    ),
    (
        r"(?i)push(?:es)? experiment (?:results|data|status)[^.\n]{0,40}\bSalesforce\b",
        "nothing in the platform calls Salesforce",
        ("docs/",),
        "When the platform pushes experiment data to Salesforce:",
    ),
    (
        r"(?i)creates an association between the PR and the experiment",
        "a pull_request delivery is answered and nothing is stored",
        ("docs/",),
        "The platform parses this field from incoming `pull_request` webhook events and creates an association between the PR and the experiment.",
    ),
    (
        r"(?i)maps Jira issue transitions to experiment lifecycle actions",
        "a Jira delivery changes no experiment",
        ("docs/",),
        "The platform maps Jira issue transitions to experiment lifecycle actions.",
    ),
    (
        r"(?i)create GitHub issues (?:directly )?from the platform",
        "nothing in the platform calls GitHub",
        ("docs/",),
        "and create GitHub issues directly from the platform.",
    ),
)

#: (page, text it must contain, why).
REQUIRED: Tuple[Tuple[str, str, str], ...] = (
    (
        "docs/integrations/github.md",
        '{"status": "received"}',
        "the webhook's success body",
    ),
    (
        "docs/integrations/github.md",
        "/api/v1/integrations/github",
        "the integration is addressed by its type",
    ),
    (
        "docs/integrations/github.md",
        '{"detail": "Webhook authentication failed"}',
        "the one body of every refused delivery",
    ),
    (
        "docs/integrations/salesforce.md",
        '{"status": "received"}',
        "the webhook's success body",
    ),
    (
        "docs/integrations/salesforce.md",
        "/api/v1/integrations/salesforce",
        "the integration is addressed by its type",
    ),
    (
        "docs/integrations/salesforce.md",
        "X-Experimently-Webhook-Secret",
        "how a Salesforce delivery authenticates; without it every delivery is a 401",
    ),
    (
        "docs/integrations/salesforce.md",
        "X-Hub-Signature-256",
        "the signed form a callout may use",
    ),
    (
        "docs/integrations/salesforce.md",
        "Flow HTTP Callout",
        "what sends to the route: a native Outbound Message cannot",
    ),
    (
        "docs/integrations/salesforce.md",
        "SOAP/XML",
        "why a native Outbound Message cannot call the route",
    ),
    (
        "docs/api/integrations.md",
        "Flow HTTP Callout",
        "what sends to the Salesforce route: a native Outbound Message cannot",
    ),
    (
        "docs/api/integrations.md",
        "Nothing in the platform calls Jira, Salesforce or GitHub yet",
        "no outbound call is wired (test_integration_wiring.py)",
    ),
    (
        "docs/integrations/github.md",
        "nothing in the platform calls GitHub yet",
        "no outbound call is wired (test_integration_wiring.py)",
    ),
    (
        "docs/integrations/salesforce.md",
        "nothing in the platform calls Salesforce yet",
        "no outbound call is wired (test_integration_wiring.py)",
    ),
    (
        "docs/getting-started/faq.md",
        "Nothing in the platform calls Jira, Salesforce or GitHub yet",
        "no outbound call is wired (test_integration_wiring.py)",
    ),
)


def _swept() -> List[Path]:
    files = sorted(DOCS.rglob("*.md"))
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in files}
    missing = [f for f in SWEEP_FLOOR if f not in rels]
    assert not missing, f"the sweep did not read {missing}: it is broken, not clean"
    return files


def _applies(rel: str, only: Optional[Tuple[str, ...]]) -> bool:
    return only is None or any(rel.startswith(prefix) for prefix in only)


def _hits(files: List[Path]) -> Iterator[str]:
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, why, only, _sample in STALE:
            if not _applies(rel, only):
                continue
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                yield f"{rel}:{line}: {match.group(0)!r} -- {why}"


def test_the_sweep_reads_the_pages() -> None:
    assert len(_swept()) > len(SWEEP_FLOOR)


def test_no_page_describes_an_integration_the_api_does_not_have() -> None:
    hits = list(_hits(_swept()))
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize(
    ("pattern", "sample", "only"),
    [(rule[0], rule[3], rule[2]) for rule in STALE],
    ids=[rule[0][:40] for rule in STALE],
)
def test_each_forbidden_pattern_matches_the_text_it_removed(
    pattern: str, sample: str, only: Optional[Tuple[str, ...]]
) -> None:
    assert re.search(pattern, sample), (
        f"{pattern!r} no longer matches the text it was written for"
    )
    if only is not None:
        # A scoped rule is scoped to pages that exist.
        for prefix in only:
            assert (REPO_ROOT / prefix).exists(), prefix


@pytest.mark.parametrize(
    ("page", "text", "why"), REQUIRED, ids=[f"{r[0]}::{r[1][:30]}" for r in REQUIRED]
)
def test_the_page_says_what_the_api_does(page: str, text: str, why: str) -> None:
    body = (REPO_ROOT / page).read_text(encoding="utf-8")
    assert text in body, f"{page} does not contain {text!r} ({why})"


# ---------------------------------------------------------------------------
# What the pages say the platform does with Jira, Salesforce and GitHub
# ---------------------------------------------------------------------------
#
# The phrase rules above catch the wordings that were removed; a paraphrase
# ("The platform creates a GitHub issue for each experiment") gets past them.
# So every sentence on these pages that puts the platform next to an action and
# a Jira, Salesforce or GitHub object is a claim, and each one has to be on the
# list below, word for word. Nothing in the platform calls those services and a
# delivery changes nothing (test_integration_wiring.py holds both), so the list
# holds only sentences that say so, or that describe the inbound direction. A
# new sentence of that shape fails here until someone reads it and lists it;
# when a call is wired, the pages and this list change together.

#: The pages whose sentences are checked against the list.
PLATFORM_PAGES = (
    "docs/api/integrations.md",
    "docs/integrations/github.md",
    "docs/integrations/salesforce.md",
    "docs/getting-started/faq.md",
)

#: A sentence is a claim when it has all three. The verbs are stems, so
#: "creates", "created" and "creating" are one entry; the actor includes
#: "automatically", the usual subject of a passive claim.
_ACTOR = re.compile(
    r"(?i)\b(?:platform|controlvar|automatically)\b"
    # An actor-less passive ("a Jira issue is created for ...") is a claim too.
    r"|\b(?:is|are|was|were|be|been|being|gets?|got)\s+(?:\w+ly\s+)?"
    r"(?:creat|push|sync|updat|link|post|comment|open|clos|transition|sent|writ"
    r"|written|notif|mark|fil)\w*"
)
_ACTION = re.compile(
    r"(?i)\b(?:creat|push|sync|updat|link|post|comment|open|clos|transition|send"
    r"|sent|writ|notif|finali[sz]|mark|map|call|act|pass|exchang|refresh|make"
    r"|made|reach|fil)\w*"
)
_REMOTE = re.compile(
    r"(?i)\b(?:jira|salesforce|github|issues?|campaigns?|opportunit(?:y|ies)"
    r"|pull requests?|PRs?|tickets?|comments?)\b"
)
_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,}).*?^\1\2[ \t]*$", re.S | re.M)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\||#|>|<)")

ALLOWED_PLATFORM_SENTENCES: Dict[str, Tuple[str, ...]] = {
    "docs/api/integrations.md": (
        "Nothing in the platform calls Jira, Salesforce or GitHub yet, and an authenticated delivery is acknowledged without changing anything in the platform (see [What a delivery does](#what-a-delivery-does)).",
        "An authenticated delivery is acknowledged, and nothing in the platform changes because of it yet: no experiment, flag or other record is created or updated from a Jira, Salesforce or GitHub event.",
        "The Jira client is written for **HTTP Basic Auth** (`email:api_token`); nothing in the platform calls it yet.",
        "A Jira project key such as `EXP`, stored for calls to Jira, which nothing in the platform makes yet",
        "The Salesforce client is written for **OAuth 2.0 Client Credentials** (`client_id` and `client_secret` for an access token at the Salesforce token endpoint); nothing in the platform calls it yet.",
        "The GitHub client is written for a **Bearer Token** (a GitHub Personal Access Token or a GitHub App installation token in `Authorization: Bearer <token>`); nothing in the platform calls it yet.",
    ),
    "docs/integrations/github.md": (
        "It also stores a token and a repository for calls to GitHub, but nothing in the platform calls GitHub yet, and a delivery is acknowledged without changing anything in the platform.",
        "- **Inbound (GitHub → Platform)**: Receive webhook events from GitHub (`push`, `pull_request`, `issues`).",
        "Nothing in the platform acts on the event yet: no pull request is linked to an experiment, and no experiment changes.",
        "Nothing in the platform calls GitHub, so the `token` and the repository are stored but not used.",
        "The token is stored for calls to GitHub, which nothing in the platform makes yet, so an integration that only receives webhooks can leave it out.",
        "GitHub PAT or GitHub App installation token, stored for calls to GitHub, which nothing in the platform makes yet.",
        "An answered delivery changes nothing in the platform yet: a pull request is not linked to an experiment, whatever its body says.",
    ),
    "docs/integrations/salesforce.md": (
        "Set the method to `POST` and the URL to your webhook URL: `https://your-platform.example.com/api/v1/integrations/webhooks/salesforce`",
        "The Salesforce integration receives the events a Salesforce Flow or Apex callout sends to the platform, and authenticates each one.",
        "It also stores your Connected App's credentials for calls to Salesforce, but nothing in the platform calls Salesforce yet, and a delivery is acknowledged without changing anything in the platform.",
        "Nothing in the platform calls Salesforce, so no experiment's status or result reaches a Salesforce record; the Connected App's credentials are stored but not used.",
        "- **Inbound (Salesforce → Platform)**: Receive events from Salesforce via webhook, posted as JSON by a Flow HTTP Callout, an Apex callout or a relay.",
        "The Connected App's credentials are stored for calls to Salesforce, which nothing in the platform makes yet, so an integration that only receives webhooks needs nothing but a `webhook_secret`.",
        "The Salesforce client in the platform is written for the **OAuth 2.0 Client Credentials** flow, and nothing calls it yet.",
        "To receive incoming events from Salesforce, configure a Salesforce Flow with an HTTP Callout (or an Apex callout, or a relay in front of the platform) to POST a JSON object to:",
        "The platform accepts JSON objects from a Salesforce Flow HTTP Callout, an Apex callout or a relay.",
        "Nothing in the platform calls Salesforce yet: no experiment's status or result is written to a Salesforce record, and the `instance_url`, `client_id` and `client_secret` are stored for a sync that is not wired.",
    ),
    "docs/getting-started/faq.md": (
        "Integrations are created by an ADMIN at `POST /api/v1/integrations`, are addressed by their type afterwards (`GET /api/v1/integrations/github`; there is one of each type), and have per-service webhook endpoints at `POST /api/v1/integrations/webhooks/github (also /jira, /salesforce)`.",
        "Nothing in the platform calls Jira, Salesforce or GitHub yet, and a delivery changes nothing in the platform:",
    ),
}


def platform_claims(text: str) -> List[str]:
    """The sentences of *text*, outside code and comments, that are claims.

    A hard-wrapped line is joined to the line it continues (any non-blank line
    that does not start a list item, a table row or a heading), then each line
    is split at table cells and after sentence-ending punctuation and its
    whitespace collapsed, so a sentence wrapped differently is the same.
    """
    text = _COMMENT.sub(" ", _FENCE.sub("\n", text))
    lines: List[str] = []
    for raw in text.splitlines():
        starts_block = not raw.strip() or bool(_BLOCK_START.match(raw))
        if lines and lines[-1].strip() and not starts_block:
            lines[-1] = f"{lines[-1]} {raw.strip()}"
        else:
            lines.append(raw)
    found = []
    for line in lines:
        for cell in line.split("|"):
            for piece in re.split(r"(?<=[.!?])\s+", cell):
                sentence = " ".join(piece.split())
                if (
                    sentence
                    and _ACTOR.search(sentence)
                    and _ACTION.search(sentence)
                    and _REMOTE.search(sentence)
                ):
                    found.append(sentence)
    return found


def test_every_claim_about_what_the_platform_does_is_on_the_list() -> None:
    assert set(ALLOWED_PLATFORM_SENTENCES) == set(PLATFORM_PAGES)
    problems = []
    for page in PLATFORM_PAGES:
        found = platform_claims((REPO_ROOT / page).read_text(encoding="utf-8"))
        allowed = ALLOWED_PLATFORM_SENTENCES[page]
        for sentence in found:
            if sentence not in allowed:
                problems.append(f"{page}: not on the list: {sentence!r}")
        for sentence in allowed:
            if sentence not in found:
                problems.append(
                    f"{page}: listed but no longer on the page: {sentence!r}"
                )
    assert not problems, (
        "\n".join(problems)
        + "\n\nA sentence that puts the platform next to an action on Jira,"
        " Salesforce or GitHub is a claim. Nothing in the platform calls them"
        " (test_integration_wiring.py): reword it, or, if it is true, list it in"
        " ALLOWED_PLATFORM_SENTENCES."
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "The platform creates a GitHub issue for each experiment.",
        "A Salesforce campaign is updated automatically when an experiment completes.",
        "Controlvar opens a pull request for every rollout.",
        "Results are pushed to Jira by the platform.",
        "| `project_key` | Default project for issues the platform creates |",
    ],
)
def test_a_reworded_claim_is_still_a_claim(sentence: str) -> None:
    claims = platform_claims(sentence)
    assert claims, f"{sentence!r} was not seen as a claim"
    listed = {s for page in ALLOWED_PLATFORM_SENTENCES.values() for s in page}
    assert not set(claims) & listed
