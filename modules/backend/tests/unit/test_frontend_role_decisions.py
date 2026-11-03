"""Every read of a user's role in the dashboard is classified (#891, check #42).

Custom roles and direct grants take effect (#891), so a dashboard control
decided by the built-in role alone hides what the API now accepts. The
permission helpers read the signed-in user's effective map instead
(``frontend/src/utils/effectivePermissions.ts``). This test finds every read
of a ``role`` property in the non-test sources of ``frontend/src`` and
``modules/frontend/src`` and holds the set to ``SITES``, where each one is
classified:

* ``CONVERTED``: it decides a grantable pair and also reads the effective map;
* ``BASE``: it decides by the built-in role on purpose, with the reason;
* ``NOTHING``: it decides no permission (a label, a colour, a form value).

A new read fails ``test_every_role_read_is_classified`` as ``unclassified``,
with its file, line and text, until it is classified here. The count is
asserted, so a read that disappears fails too.

What counts as a read (comments are stripped first; strings are kept, so a
read inside a template literal is seen):

* a property access: ``x.role``, ``x?.role``, ``x!.role``, ``x['role']``;
* a destructuring: ``const { role } = x``;
* a bare ``role`` compared or looked up: ``role === 'ADMIN'``,
  ``list.includes(role)``, ``list.indexOf(String(role))``, ``set.has(role)``,
  ``switch (role)``.

A type's indexed access (``WorkspaceMember['role']``, capitalised) is not a
read. Entries are keyed by file and whitespace-normalised line, with how many
reads that line holds, so an edit to a classified line makes it unclassified
again. It reads ``modules/``, so it is a modules test.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
ROOTS = ("frontend/src", "modules/frontend/src")
SUFFIXES = (".ts", ".tsx")

CONVERTED = "converted to the effective map"
BASE = "base role by design"
NOTHING = "decides nothing"

Key = Tuple[str, str]


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return "tests" in parts or "__mocks__" in parts or ".test." in name


def source_files(root: Path = REPO) -> Iterator[Tuple[str, Path]]:
    """The non-test ``.ts``/``.tsx`` files under ``ROOTS``, walked without git."""
    for base in ROOTS:
        top = root / base
        if not top.is_dir():
            continue
        for current, dirs, names in os.walk(top):
            dirs[:] = sorted(
                d for d in dirs if d not in {"node_modules", ".next", "out"}
            )
            for name in sorted(names):
                path = Path(current) / name
                rel = path.relative_to(root).as_posix()
                if path.suffix in SUFFIXES and not _is_test(rel):
                    yield rel, path


def strip_comments(text: str) -> str:
    """*text* with every ``//`` and ``/* */`` comment blanked, newlines kept.

    Strings and template literals are kept whole (a template's ``${...}`` is
    code again), so ``'https://'`` and `` `${user.role}` `` survive. A ``//``
    right after a ``:`` (a URL in JSX text) is not a comment. A quote that
    opens a string the scanner cannot close (an apostrophe in JSX text) ends
    at the line's end, so it can hide nothing beyond its line.
    """
    out: List[str] = []
    i, n = 0, len(text)
    state = ""  # "", "'", '"', "`"
    templates: List[int] = []  # brace depth inside each open `${`
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state in ("'", '"'):
            if c == "\\":
                out.append(text[i : i + 2])
                i += 2
                continue
            out.append(c)
            if c == state or c == "\n":
                state = ""
            i += 1
            continue
        if state == "`":
            if c == "\\":
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == "$" and nxt == "{":
                out.append("${")
                templates.append(0)
                state = ""
                i += 2
                continue
            out.append(c)
            if c == "`":
                state = ""
            i += 1
            continue
        if c == "/" and nxt == "/" and (i == 0 or text[i - 1] != ":"):
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("\n" * text.count("\n", i, end))
            i = end
            continue
        if c in "'\"`":
            state = c
            out.append(c)
            i += 1
            continue
        if c == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if templates and c == "{":
            templates[-1] += 1
        elif templates and c == "}":
            if templates[-1] == 0:
                templates.pop()
                state = "`"
            else:
                templates[-1] -= 1
        out.append(c)
        i += 1
    return "".join(out)


_PROPERTY = re.compile(r"(?:\?\.|\.)\s*(role)(?![\w$])")
_BRACKET = re.compile(r"([A-Za-z_$][\w$]*|[)\]])\s*(?:\?\.)?\[\s*(['\"])(role)\2\s*\]")
_DESTRUCTURE = re.compile(r"\{[^{};]*?(?<![\w$.])(role)(?![\w$])[^{};]*\}\s*=(?![=>])")
_BARE = re.compile(
    r"(?<![\w$.])(role)\s*(?:===|!==|==|!=)"
    r"|(?:===|!==|==|!=)\s*(role)(?![\w$.])"
    r"|\.(?:includes|indexOf|has)\(\s*(?:String\(\s*)?(role)(?![\w$])"
    r"|\bswitch\s*\(\s*(role)\s*\)"
)


def _role_offsets(code: str) -> List[int]:
    """The offset of each ``role`` token that is read, each counted once."""
    found = set()
    for m in _PROPERTY.finditer(code):
        k = m.start() - 1
        while k >= 0 and code[k] in " \t\r\n":
            k -= 1
        before = code[k] if k >= 0 else ""
        # `...role` (a spread) and `..role` are not reads of a property.
        if before and (before.isalnum() or before in "_$)]!"):
            found.add(m.start(1))
    for m in _BRACKET.finditer(code):
        base = m.group(1)
        if base[0].isupper():  # a type's indexed access, `Member['role']`
            continue
        found.add(m.start(3))
    for m in _DESTRUCTURE.finditer(code):
        found.add(m.start(1))
    for m in _BARE.finditer(code):
        for g in range(1, 5):
            if m.group(g) is not None:
                found.add(m.start(g))
    return sorted(found)


def role_reads_in(text: str) -> List[Tuple[int, str]]:
    """Each read in *text* as (line number, the line normalised)."""
    code = strip_comments(text)
    lines = code.split("\n")
    reads = []
    for offset in _role_offsets(code):
        line_no = code.count("\n", 0, offset) + 1
        reads.append((line_no, " ".join(lines[line_no - 1].split())))
    return reads


def scan(root: Path = REPO) -> Tuple[Counter, Dict[Key, List[int]]]:
    """Every read under *root*: a count per (file, line), and where each is."""
    counts: Counter = Counter()
    where: Dict[Key, List[int]] = {}
    for rel, path in source_files(root):
        for line_no, line in role_reads_in(path.read_text("utf-8")):
            counts[(rel, line)] += 1
            where.setdefault((rel, line), []).append(line_no)
    return counts, where


# ---------------------------------------------------------------------------
# The classification
# ---------------------------------------------------------------------------

_APP_SHELL = "frontend/src/components/AppShell.tsx"
_REQUIRE_AUTH = "frontend/src/components/RequireAuth.tsx"
_EDIT_USER = "frontend/src/components/admin/users/EditUserModal.tsx"
_USER_TABLE = "frontend/src/components/admin/users/UserTable.tsx"
_EXPERIMENT_PAGE = "frontend/src/pages/experiments/[id].tsx"
_EXP_PERMS = "frontend/src/utils/experimentPermissions.ts"
_CONNECTION_FORM = "modules/frontend/src/components/warehouse/ConnectionForm.tsx"
_SOURCE_FORM = "modules/frontend/src/components/warehouse/SourceForm.tsx"
_MEMBERS = "modules/frontend/src/pages/workspaces/[id]/members.tsx"
_WH_ROLES = "modules/frontend/src/services/warehouseRoles.ts"
_WH_RUNS = "modules/frontend/src/services/warehouseRuns.ts"

_WORKSPACE = (
    "workspace member roles, a separate model the workspaces API decides "
    "(class B, plan 3.9); no grantable pair names it"
)
_WAREHOUSE = (
    "the warehouse role bands read the built-in role (class B, plan 3.7 and 3.9); "
    "no grantable pair names a warehouse action"
)
_SNOWFLAKE = "a Snowflake role name typed into the connection form, not a user's role"
_COLUMN = "a warehouse source column's role (what the column holds), not a user's role"

#: (file, normalised line) -> (reads on that line, class, reason).
SITES: Dict[Key, Tuple[int, str, str]] = {
    # --- the permission helpers, converted (plan 3.7) ---------------------
    (
        _EXP_PERMS,
        "ROLES_THAT_CAN_CREATE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||",
    ): (
        1,
        CONVERTED,
        "canCreateExperiment and canReadEligibility: experiment:create",
    ),
    (
        _EXP_PERMS,
        "ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||",
    ): (
        2,
        CONVERTED,
        "canChangeExperiment (experiment:update) and canDeleteExperiment (experiment:delete)",
    ),
    (
        _EXP_PERMS,
        "FLAG_ROLES_THAT_CAN_CHANGE.includes(user.role as 'ADMIN' | 'DEVELOPER') ||",
    ): (
        1,
        CONVERTED,
        "canCreateFeatureFlags (feature_flag:create) and canChangeFeatureFlags (feature_flag:update)",
    ),
    (
        "frontend/src/utils/segmentPermissions.ts",
        "return ROLES_THAT_CAN_CHANGE.indexOf(String(user.role)) !== -1 || holds(effective, 'experiment', action);",
    ): (
        1,
        CONVERTED,
        "canCreateSegments, canChangeSegments, canArchiveSegments: experiment create / update / delete",
    ),
    (
        "frontend/src/components/admin/api-keys/CreateApiKeyModal.tsx",
        "return ROLES_THAT_CHANGE_FLAGS.includes(user.role) || holds(effective, 'feature_flag', 'update');",
    ): (
        1,
        CONVERTED,
        "canCreateRulesetKey: feature_flag:update (option B: the server-key rule is flag update only)",
    ),
    (
        "frontend/src/components/admin/audit/access.ts",
        "return user.is_superuser === true || READ_ALL_ROLES.includes(user.role) || holds(effective, 'audit_log', 'read');",
    ): (1, CONVERTED, "readsAllAuditLogs: audit_log:read"),
    # --- decided by the built-in role on purpose ---------------------------
    (
        _EXP_PERMS,
        "return ROLES_THAT_ADMINISTER_ELIGIBILITY.includes(user.role as 'ADMIN');",
    ): (
        1,
        BASE,
        "canAdministerEligibility mirrors _require_admin (ADMIN or a superuser), "
        "which no grantable pair names",
    ),
    (
        "frontend/src/contexts/AuthContext.tsx",
        "(...roles: Role[]): boolean => !!user && roles.includes(user.role),",
    ): (
        1,
        BASE,
        "hasRole answers 'does the user hold this built-in role', a question about the "
        "role, not a permission; no dashboard control calls it",
    ),
    (
        _APP_SHELL,
        "if (item.roles) return user !== null && item.roles.includes(user.role);",
    ): (
        1,
        BASE,
        "the Audit Log nav item: AUDIT_LOG_ROLES is all four built-in roles (every role "
        "reads at least its own entries); the list keeps it off anonymous renders",
    ),
    (
        _APP_SHELL,
        "return !!user && (user.is_superuser === true || item.roles.includes(user.role));",
    ): (1, BASE, f"the Warehouse nav item: {_WAREHOUSE}"),
    (
        _REQUIRE_AUTH,
        "if (roles && roles.length > 0 && !roles.includes(user.role)) {",
    ): (
        1,
        BASE,
        "the page guard: its only role list is the Audit Log's, all four built-in roles; "
        "the admin pages are gated on the superuser flag; no grantable pair names a page",
    ),
    (_WH_ROLES, "return role !== null && CAPABILITIES[action].roles.includes(role);"): (
        2,
        BASE,
        f"can(): {_WAREHOUSE}",
    ),
    (_WH_ROLES, "return user.role ?? null;"): (
        1,
        BASE,
        f"warehouseRole, which can() reads: {_WAREHOUSE}",
    ),
    (_WH_RUNS, "return role === 'ADMIN' || role === 'DEVELOPER';"): (
        2,
        BASE,
        f"canStartRun: {_WAREHOUSE}",
    ),
    (_WH_RUNS, "return user.role ? String(user.role).toUpperCase() : null;"): (
        2,
        BASE,
        f"effectiveRole, which canStartRun reads: {_WAREHOUSE}",
    ),
    (
        _MEMBERS,
        "const ownerCount = members.filter((m) => m.role === 'OWNER').length;",
    ): (1, BASE, f"counts owners so the last one is not demoted: {_WORKSPACE}"),
    (
        _MEMBERS,
        "const isLastOwner = member.role === 'OWNER' && ownerCount === 1;",
    ): (1, BASE, f"the last owner's role is not offered for change: {_WORKSPACE}"),
    (_MEMBERS, "{member.role === 'OWNER' ? ("): (
        1,
        BASE,
        f"an owner's role is shown, not offered for change: {_WORKSPACE}",
    ),
    # --- decides nothing ----------------------------------------------------
    (
        _APP_SHELL,
        "className={`inline-flex shrink-0 items-center px-2 py-0.5 rounded-full text-[11px] "
        "font-semibold whitespace-nowrap ${ROLE_COLORS[user.role]}`}",
    ): (1, NOTHING, "the header's role chip: its colour"),
    (_APP_SHELL, "{USER_ROLE_LABELS[user.role]}"): (
        1,
        NOTHING,
        "the header's role chip: its label",
    ),
    (
        _REQUIRE_AUTH,
        'Signed in as <span className="font-medium">{user.email}</span> ({user.role}). This page',
    ): (1, NOTHING, "names the role in the guard's 403 view"),
    (_EDIT_USER, "if (role !== null && role !== user.role) changes.role = role;"): (
        4,
        NOTHING,
        "the edit form's diff: whether the PATCH carries the role picked",
    ),
    (_EDIT_USER, "setRole(user.role);"): (
        1,
        NOTHING,
        "fills the edit form's role picker",
    ),
    (_EDIT_USER, "{role === null && ("): (
        1,
        NOTHING,
        "offers 'No role set' in the picker for a legacy account",
    ),
    (
        "frontend/src/components/admin/users/InviteUserModal.tsx",
        "is_superuser: role === 'ADMIN',",
    ): (
        1,
        NOTHING,
        "a value the invite form sends (an invited ADMIN is made a superuser); it "
        "decides nothing the signed-in user may do",
    ),
    (
        _USER_TABLE,
        "className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium "
        "${ROLE_COLORS[user.role]}`}",
    ): (1, NOTHING, "a row's role chip: its colour"),
    (_USER_TABLE, "{USER_ROLE_LABELS[user.role]}"): (
        1,
        NOTHING,
        "a row's role chip: its label",
    ),
    (_USER_TABLE, "{user.role ? ("): (
        1,
        NOTHING,
        "a row shows its role chip, or none for a legacy account",
    ),
    (_EXPERIMENT_PAGE, "role={user?.role}"): (
        1,
        NOTHING,
        "TargetingSection names the role in its note; mayChange decides",
    ),
    (_EXPERIMENT_PAGE, "you are {user?.role || 'signed in without a role'}.{' '}"): (
        1,
        NOTHING,
        "names the role in the lifecycle note; mayChange decides",
    ),
    (_CONNECTION_FORM, "put('role', checkSnowflakeRole(v.role));"): (
        1,
        NOTHING,
        _SNOWFLAKE,
    ),
    (_CONNECTION_FORM, "role: v.role,"): (1, NOTHING, _SNOWFLAKE),
    (_SOURCE_FORM, "clear(`columns.${c.role}`);"): (1, NOTHING, _COLUMN),
    (_SOURCE_FORM, "data-field={`columns.${c.role}`}"): (1, NOTHING, _COLUMN),
    (_SOURCE_FORM, "error={errors[`columns.${c.role}`]}"): (1, NOTHING, _COLUMN),
    (
        _SOURCE_FORM,
        "for (const c of columnRoles) put(`columns.${c.role}`, "
        "checkColumn(columns[c.role] ?? '', type, c.label));",
    ): (2, NOTHING, _COLUMN),
    (_SOURCE_FORM, "id={`wh-src-col-${c.role}`}"): (1, NOTHING, _COLUMN),
    (_SOURCE_FORM, "key={c.role}"): (1, NOTHING, _COLUMN),
    (_SOURCE_FORM, "setColumns((prev) => ({ ...prev, [c.role]: value }));"): (
        1,
        NOTHING,
        _COLUMN,
    ),
    (_SOURCE_FORM, "value={columns[c.role] ?? ''}"): (1, NOTHING, _COLUMN),
    (_MEMBERS, "const badge = ROLE_BADGE[member.role];"): (
        1,
        NOTHING,
        "a workspace member's badge",
    ),
    (_MEMBERS, "value={member.role}"): (
        1,
        NOTHING,
        "the member role picker's value",
    ),
    (
        "modules/frontend/src/pages/workspaces/invites/[token].tsx",
        '<span className="font-medium">{invite.role}</span>.',
    ): (1, NOTHING, "names the workspace role an invitation gives"),
}

#: Reads per class, and in all: a classification that loses or gains one fails.
EXPECTED_READS = {CONVERTED: 7, BASE: 15, NOTHING: 29}
EXPECTED_TOTAL = 51


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def differences(counts: Counter, where: Dict[Key, List[int]]) -> List[str]:
    problems = []
    for key in sorted(set(counts) | set(SITES)):
        want = SITES[key][0] if key in SITES else 0
        got = counts.get(key, 0)
        if got > want:
            lines = ",".join(str(n) for n in where.get(key, []))
            problems.append(f"unclassified: {key[0]}:{lines}: {key[1]}")
        elif got < want:
            problems.append(
                f"classified but not found ({want - got} of {want}): {key[0]}: {key[1]}"
            )
    return problems


def test_every_role_read_is_classified():
    counts, where = scan()
    problems = differences(counts, where)
    assert not problems, "\n".join(problems)


def test_the_classification_counts():
    by_class: Counter = Counter()
    for reads, cls, _ in SITES.values():
        by_class[cls] += reads
    assert dict(by_class) == EXPECTED_READS
    assert sum(by_class.values()) == EXPECTED_TOTAL
    counts, _ = scan()
    assert sum(counts.values()) == EXPECTED_TOTAL


def test_every_entry_has_a_class_and_a_reason():
    for key, (reads, cls, reason) in SITES.items():
        assert reads >= 1, key
        assert cls in (CONVERTED, BASE, NOTHING), key
        assert reason.strip(), key


def test_a_converted_read_also_reads_the_effective_map():
    # "Converted" is checked, not asserted: the line, or the one after it
    # (`ROLE_LIST.includes(...) ||` then `holds(effective, ...)`), asks the map.
    _, where = scan()
    for (rel, line), (_, cls, _) in SITES.items():
        if cls != CONVERTED:
            continue
        text = (REPO / rel).read_text("utf-8").split("\n")
        for line_no in where.get((rel, line), []):
            window = " ".join(text[line_no - 1 : line_no + 1])
            assert "holds(effective" in window, (
                f"{rel}:{line_no} reads no effective map: {line}"
            )


# ---------------------------------------------------------------------------
# The scanner's own controls, on text made here
# ---------------------------------------------------------------------------


def _lines(text: str) -> List[str]:
    return [line for _, line in role_reads_in(text)]


def test_the_two_planted_gates_are_reads():
    planted = (
        "export function a(user: U) {\n"
        "  if (user.role === 'DEVELOPER') return true;\n"
        "  return ['ADMIN'].indexOf(user.role) !== -1;\n"
        "}\n"
    )
    assert _lines(planted) == [
        "if (user.role === 'DEVELOPER') return true;",
        "return ['ADMIN'].indexOf(user.role) !== -1;",
    ]


def test_every_shape_of_read_is_found():
    text = (
        "const a = user?.role;\n"
        "const b = user!.role;\n"
        "const c = user\n  .role;\n"
        "const d = user['role'];\n"
        "const { id, role } = user;\n"
        "if (role === 'ADMIN') go();\n"
        "if ('ADMIN' !== role) go();\n"
        "ok = ROLES.includes(role);\n"
        "ok = ROLES.indexOf(String(role)) !== -1;\n"
        "ok = SET.has(role);\n"
        "switch (role) {}\n"
        "const e = `you are ${user.role}`;\n"
    )
    assert _lines(text) == [
        "const a = user?.role;",
        "const b = user!.role;",
        ".role;",
        "const d = user['role'];",
        "const { id, role } = user;",
        "if (role === 'ADMIN') go();",
        "if ('ADMIN' !== role) go();",
        "ok = ROLES.includes(role);",
        "ok = ROLES.indexOf(String(role)) !== -1;",
        "ok = SET.has(role);",
        "switch (role) {}",
        "const e = `you are ${user.role}`;",
    ]


def test_what_is_not_a_read():
    text = (
        "// if (user.role === 'ADMIN') this is a comment\n"
        "/* user.role in a block\n comment */\n"
        "type R = WorkspaceMember['role'];\n"
        "const x = { role: 'ADMIN' };\n"
        "const y = item.roles;\n"
        "const z = { ...role };\n"
        '<div role="dialog" />\n'
        "const u = 'https://example.com'; const n = 1;\n"
        "const roleName = 'x';\n"
    )
    assert _lines(text) == []


def test_a_url_or_an_apostrophe_hides_nothing_after_it():
    # `//` inside a string, or after `:` in JSX text, is not a comment; an
    # apostrophe in JSX text ends at its line.
    text = (
        "const u = 'http://x'; if (user.role === 'A') go();\n"
        "<p>See https://x {user.role}</p>\n"
        "<p>Don't {user.role}</p>\n"
        "const after = user.role;\n"
    )
    assert len(_lines(text)) == 4


def test_tests_are_not_sources():
    assert _is_test("frontend/src/tests/pages/x.test.tsx")
    assert _is_test("modules/frontend/src/rbac.test.ts")
    assert _is_test("frontend/src/__mocks__/recharts.tsx")
    assert not _is_test("frontend/src/utils/experimentPermissions.ts")
