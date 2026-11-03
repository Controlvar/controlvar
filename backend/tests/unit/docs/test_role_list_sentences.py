"""Every sentence that lists the roles allowed to do something is classified (#891, check #54).

When custom roles and direct grants take effect, "the ADMIN and DEVELOPER
roles may create experiments" stops being the whole answer: anyone whose
custom role or direct grant gives ``experiment (create)`` may too. The #891
UX delta classified every such sentence (class A, rewritten; class B, a
decision no grant can change, left as it is). This test keeps it so.

Over the docs, ``frontend/src`` and ``backend/app`` (the modules' trees are
held by ``test_role_list_sentences_modules.py``), with each file's whitespace
collapsed so a sentence wrapped over lines is one, every match of
:data:`PATTERN` outside tests and generated fixtures must either carry a grant
marker (:data:`MARKERS`: the sentence says "permission to" or "permissions,
which the", names the custom role or grant, or gives the roles only as an
example) or be one of the sentences in :data:`CLASS_B`, by file and sentence.
A count cannot be the measure: the rewritten texts keep "ADMIN and DEVELOPER"
on purpose.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

import pytest

from backend.tests.unit.docs.test_no_display_only_rbac_claims import _sentence

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]

#: The EM's regex, and the word orders the UX delta found it misses.
PATTERN = re.compile(
    r"ADMIN,? (?:and|or) DEVELOPER"
    r"|ADMIN, DEVELOPER (?:or|and) (?:a )?superuser"
    r"|DEVELOPER (?:or|and) ADMIN"
    r"|ADMIN, DEVELOPER, (?:or )?(?:a )?superuser"
    r"|an ADMIN, a DEVELOPER"
    r"|(?:ANALYST|DEVELOPER),? (?:and|or) VIEWER",
    re.IGNORECASE,
)
MARKERS = (
    # The sentence names the custom role or grant that also admits a user.
    "custom role",
    "direct grant",
    "or grant",
    "the effective",
    "effective map",
    # It names the permission first, then the built-in roles that have it.
    "permission to",
    "permissions, which the",
    # It gives the roles only as an example.
    "such as",
)
# No marker is a phrase a sentence that still needs rewriting can carry by
# accident: "who can change feature flags (the ADMIN and DEVELOPER roles)" and
# "access is by role" carry "who can " and "by role" and are exactly what this
# test exists to report. A rewritten sentence that says what the role alone
# does ("by their role"), or names the permission as "users who can change
# ...", is listed in CLASS_B with the R-text that rewrote it.
#: In Python, adjacent string literals ("a " "b", or "a " f"b"), once the
#: whitespace is collapsed, are one sentence.
_PY_LITERALS = re.compile(r"([\"']) f?\1")
TREES = ("docs", "frontend/src", "backend/app")
SUFFIXES = frozenset({".md", ".py", ".ts", ".tsx", ".js", ".jsx"})
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "_claims", "tests", "__tests__"})


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return ".test." in name or name.startswith("test_") or "/tests/" in f"/{rel}"


def _files(trees) -> Iterator[Tuple[str, Path]]:
    for tree in trees:
        root = REPO / tree
        for top, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                path = Path(top) / name
                rel = path.relative_to(REPO).as_posix()
                if path.suffix in SUFFIXES and not _is_test(rel):
                    yield rel, path


def unmarked(text: str, python: bool = False) -> Set[str]:
    """The sentences of *text* that list roles and carry no grant marker."""
    text = " ".join(text.split())
    if python:
        text = _PY_LITERALS.sub("", text)
    found = set()
    for match in PATTERN.finditer(text):
        sentence = _sentence(text, match.start(), match.end())
        lower = sentence.lower()
        if not any(marker in lower for marker in MARKERS):
            found.add(sentence)
    return found


def scan(trees) -> Set[Tuple[str, str]]:
    hits = set()
    for rel, path in _files(trees):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits |= {(rel, s) for s in unmarked(text, python=rel.endswith(".py"))}
    return hits


#: Class B: role-list sentences about decisions no custom role or grant can
#: change (administration, the compliance trail, the split-URL preview,
#: interaction analysis, notifications, warehouse bands, workspace roles), or
#: that only name the four roles; and class-A sentences, already rewritten,
#: that no marker sees, each with the R-text that rewrote it. (file, sentence),
#: whitespace collapsed.
CLASS_B: Dict[Tuple[str, str], str] = {
    (
        "backend/app/api/v1/endpoints/api_keys.py",
        "Only users who can change feature flags can create an API key with the "
        "{SDK_RULESET_SCOPE} scope; the ADMIN and DEVELOPER roles and superusers "
        "can.",
    ): "class A, rewritten (R2): names the permission, then the built-in roles that have it",
    (
        "backend/app/core/permissions.py",
        "Access to feature flags is by permission, not by who # created the flag: "
        "ADMIN and DEVELOPER act on any flag; ANALYST and VIEWER read # any flag "
        "and, by their role, change none (ROLE_PERMISSIONS gives them no UPDATE # "
        "or DELETE).",
    ): "class A, rewritten (R8): what the role alone does; the next sentence gives the grant",
    (
        "docs/api/endpoints.md",
        "**Regular Users**: - Can access their own data - Cannot access admin "
        "endpoints - Feature flags are by permission, not ownership: ADMIN and "
        "DEVELOPER may read, create, change and delete any flag; ANALYST and VIEWER "
        "may read any flag and, by their role, change none.",
    ): "class A, rewritten (R13): what the role alone does; the next sentence gives the grant",
    (
        "docs/api/endpoints.md",
        "ANALYST and VIEWER change no experiment by their role, including one they own.",
    ): "class A, rewritten (R14): what the role alone does; the next sentence gives the grant",
    (
        "docs/api/stability.md",
        "The 403 for an owner who cannot change feature flags now reads `This key's "
        "owner can no longer change feature flags, so the key is refused for "
        "server-side SDK use.`, and the key-creation 403 reads `Only users who can "
        "change feature flags can create an API key with the sdk:ruleset scope; the "
        "ADMIN and DEVELOPER roles and superusers can.",
    ): "quotes R2, the key-creation refusal, word for word",
    (
        "docs/rbac/README.md",
        "**Feature flags.** A flag's owner is recorded and shown, but access to "
        "flags is decided by permission, never by ownership: ADMIN and DEVELOPER "
        "may change any flag, and ANALYST and VIEWER may change none by their role, "
        "including one they own.",
    ): "class A, rewritten (R28): what the role alone does; the next sentence gives the grant",
    (
        "docs/rbac/README.md",
        "Feature flags are governed by permission, not by who created them: an owner "
        "is recorded on each flag and shown, but owning a flag does not let an "
        "Analyst or Viewer change it.",
    ): "owning a flag admits no one; the overview says what a role cannot do describes the role alone",
    (
        "frontend/src/components/admin/api-keys/CreateApiKeyModal.tsx",
        "Only users who can change feature flags can create a key with this scope; "
        "the ADMIN and DEVELOPER roles and superusers can.'",
    ): "class A, rewritten (R34): names the permission, then the built-in roles that have it",
    (
        "backend/app/api/v1/endpoints/compliance.py",
        "DEVELOPER and VIEWER roles will receive HTTP 403.",
    ): "the compliance audit events stay with the built-in role",
    (
        "backend/app/api/v1/endpoints/experiments.py",
        "For ADMIN and DEVELOPER users (and superusers).",
    ): "the split-URL preview stays with the built-in role",
    (
        "backend/app/api/v1/endpoints/experiments.py",
        'Only DEVELOPER or ADMIN users can access the split URL preview endpoint"',
    ): "the split-URL preview stays with the built-in role",
    (
        "backend/app/api/v1/endpoints/interactions.py",
        "Access: ANALYST, DEVELOPER or ADMIN (VIEWER gets 403).",
    ): "interaction analysis stays with the built-in role",
    (
        "backend/app/api/v1/endpoints/interactions.py",
        'Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role."',
    ): "interaction analysis stays with the built-in role",
    (
        "backend/app/api/v1/endpoints/scheduler_health.py",
        "Accessible by ADMIN and DEVELOPER roles.",
    ): "the scheduler health routes check no permission; outside #891",
    (
        "backend/app/schemas/user.py",
        "RBAC role name (ADMIN, DEVELOPER, ANALYST or VIEWER; case-insensitive).",
    ): "only names the four roles",
    (
        "backend/app/schemas/user.py",
        'RBAC role name: ADMIN, DEVELOPER, ANALYST or VIEWER (case-insensitive)."',
    ): "only names the four roles",
    (
        "docs/api/rbac.md",
        "Without it, Developer and Viewer users read only their own entries.",
    ): "class A in context: the audit_log (read) row, whose first sentence is the grant",
    (
        "docs/api/alerting.md",
        "Requires the DEVELOPER or ADMIN role.",
    ): "notifications (the test notification) stay with the built-in role",
    (
        "docs/api/compliance.md",
        "ADMIN or ANALYST (or a superuser); DEVELOPER and VIEWER get `403`.",
    ): "the compliance trail stays with the built-in role",
    (
        "docs/api/endpoints.md",
        "ADMIN and DEVELOPER may create experiments, clone any experiment, and "
        "update, start, pause, complete, archive or annotate (`metadata`) any "
        "experiment, schedule any experiment, and delete any experiment in DRAFT.",
    ): "class A in context (R14): the sentences after it give the grant clause",
    (
        "docs/api/endpoints.md",
        "Access: ANALYST, DEVELOPER or ADMIN (VIEWER returns 403).",
    ): "interaction analysis stays with the built-in role",
    (
        "docs/api/endpoints.md",
        "text POST /api/v1/integrations — Create integration (ADMIN) GET "
        "/api/v1/integrations — List integrations (ADMIN or DEVELOPER) GET "
        "/api/v1/integrations/{integration_type} — Get integration details "
        "(ADMIN or DEVELOPER) PUT /api/v1/integrations/{integration_type} — "
        "Update integration config (ADMIN) DELETE "
        "/api/v1/integrations/{integration_type} — Delete integration (ADMIN) ```",
    ): "module administration (integrations) stays with the built-in role",
    (
        "docs/api/integrations.md",
        "**Authentication**: ADMIN or DEVELOPER.",
    ): "module administration (integrations) stays with the built-in role",
    (
        "docs/api/integrations.md",
        "**Authentication**: the CRUD endpoints require a Bearer token — **ADMIN "
        "or DEVELOPER** to read, **ADMIN** to create, update or delete.",
    ): "module administration (integrations) stays with the built-in role",
    (
        "docs/api/interaction-detection.md",
        "`Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role.` |",
    ): "interaction analysis stays with the built-in role",
    (
        "docs/api/split-url.md",
        "It is for ADMIN and DEVELOPER users (and superusers), and in a core "
        "deployment it answers `501`.",
    ): "the split-URL preview stays with the built-in role",
    (
        "docs/api/split-url.md",
        "Preview: the user is not ADMIN, DEVELOPER or a superuser |",
    ): "the split-URL preview stays with the built-in role",
    (
        "docs/auth/auth-user-guide.md",
        "The four roles are Admin, Developer, Analyst and Viewer.",
    ): "only names the four roles",
    (
        "docs/security/api-keys.md",
        "Using such a key follows the same rule: the three routes above accept a "
        "key with `sdk:ruleset` only while its owner can change feature flags, "
        "checked on every request, so a key whose owner loses that (moved to "
        "ANALYST or VIEWER, or a grant revoked or expired) is refused with `403` "
        "from then on.",
    ): "class A, rewritten (R32): names the permission and the grant",
    (
        "docs/workspaces/overview.md",
        "### Changing Roles - An ADMIN or OWNER can change a member's role among "
        "VIEWER, ANALYST, DEVELOPER and ADMIN.",
    ): "workspace member roles are a separate model",
    (
        "docs/workspaces/overview.md",
        "DEVELOPER, ANALYST and VIEWER are recorded for each member but currently "
        "grant the same thing on the workspace routes.",
    ): "workspace member roles are a separate model",
    (
        "frontend/src/components/admin/withAdminGuard.tsx",
        "` admits DEVELOPER and ADMIN.",
    ): "the admin area's guard option, described (superuser-only pages)",
    (
        "frontend/src/utils/experimentPermissions.ts",
        "Who created the experiment is not * part of the question, so an ANALYST "
        "or VIEWER without that permission is * refused on an experiment they own "
        "as well.",
    ): "class A in context: the comment above it names the effective map",
}


def test_every_role_list_sentence_is_classified():
    hits = scan(TREES)
    unclassified = sorted(hits - set(CLASS_B))
    assert unclassified == [], (
        "a sentence lists the roles allowed to do something and names no custom "
        "role or grant; rewrite it (the #891 replacement rule) or, if no grant "
        f"can change the decision, add it to CLASS_B: {unclassified}"
    )
    gone = sorted(set(CLASS_B) - hits)
    assert gone == [], f"CLASS_B entries no longer in the tree: {gone}"


def test_an_unmarked_sentence_is_found_and_a_marked_one_is_not():
    assert unmarked("Cloning an experiment requires the ADMIN or DEVELOPER role.") == {
        "Cloning an experiment requires the ADMIN or DEVELOPER role."
    }
    assert (
        unmarked(
            "Cloning an experiment needs permission to create experiments, which the "
            "ADMIN and DEVELOPER roles have."
        )
        == set()
    )
    # A sentence wrapped over two lines is one sentence.
    assert unmarked("Only DEVELOPER\n   or ADMIN users can do it.") == {
        "Only DEVELOPER or ADMIN users can do it."
    }


#: Sentences the tree held before the #891 sweep that say "who can" or "by
#: role" and still needed rewriting (R2, R34, the flag delete docstring, the
#: RBAC overview). Each must be reported.
BEFORE_THE_SWEEP = (
    "Only users who can change feature flags (the ADMIN and DEVELOPER roles, or a "
    "superuser) can create an API key with the sdk:ruleset scope.",
    "Only users who can change feature flags (the ADMIN and DEVELOPER roles, or a "
    "superuser) can create a key with this scope.",
    "Access is by role: ADMIN and DEVELOPER may delete any flag; ANALYST and VIEWER "
    "may not.",
    "Feature flags are governed by role, not by who created them: an owner is "
    "recorded on each flag and shown, but owning a flag does not let an Analyst or "
    "Viewer change it.",
)


@pytest.mark.parametrize("sentence", BEFORE_THE_SWEEP)
def test_a_sentence_from_before_the_sweep_is_reported(sentence):
    assert unmarked(sentence) == {sentence}
