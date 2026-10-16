"""`scripts/doc_examples.py` — the contract for the documentation's shell examples.

Every refusal is driven with a fixture page; the render rules are driven with
HTML produced by the same Markdown extensions MkDocs uses (read from
``mkdocs.yml``), never hand-written HTML; and the real repository's enrolment is
checked as it stands, so a page edit that breaks the contract fails here too.
No Docker and no network, and git only in repositories that the demotion-guard
tests create under ``tmp_path`` (never this checkout's): the repository root
comes from this file's path, so the tests also run in ``scripts/core_build.sh``'s
copy.
"""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import textwrap
import threading

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO = pathlib.Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "doc_examples", REPO / "scripts" / "doc_examples.py"
)
dx = importlib.util.module_from_spec(_spec)
sys.modules["doc_examples"] = dx  # dataclasses look their module up while loading
_spec.loader.exec_module(dx)

FENCE = "`" * 3


def page(*blocks: str) -> str:
    return (
        "# Page\n\n"
        + "\n\n".join(textwrap.dedent(b).strip("\n") for b in blocks)
        + "\n"
    )


def blocks(text: str):
    return [
        b
        for f in dx.parse_fences(text, "p.md")
        if (b := dx.classify(f, "p.md")) is not None
    ]


def refused(text: str) -> str:
    with pytest.raises(dx.Refused) as excinfo:
        for block in blocks(text):
            dx.lint_block(block, "p.md")
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# The tag grammar
# ---------------------------------------------------------------------------


def test_exec_and_skip_blocks_are_read_with_their_expectations():
    text = page(
        f"""
        {FENCE}{{.bash exec timeout=30}}
        echo hi
        {FENCE}
        <!-- expect: hi -->
        <!-- expect: there -->
        """,
        f"""
        {FENCE}{{.bash skip reason="demo: needs a browser"}}
        open http://localhost:3000
        {FENCE}
        """,
        f"""
        {FENCE}json
        {{"a": 1}}
        {FENCE}
        """,
    )
    run, skip = blocks(text)
    assert (run.kind, run.timeout, run.expects) == ("exec", 30, ["hi", "there"])
    assert (skip.kind, skip.reason) == ("skip", "demo: needs a browser")


@pytest.mark.parametrize(
    "info, message",
    [
        ("bash", "untagged shell fence"),
        ("sh", "'sh' names a shell the runner does not know"),
        ("bash exec", "not brace form"),
        ("{bash exec}", "without '.'"),
        ("{.bash exec} trailing", "text after '}'"),
        ("{.bash exec skip}", "exactly one of exec or skip"),
        ("{.bash}", "exactly one of exec or skip"),
        ("{.bash skip}", 'skip needs reason="..."'),
        ('{.bash exec reason="x"}', "reason on a block that runs"),
        ("{.bash exec title=x}", "unknown attribute"),
        ('{.bash exec hl_lines="1"}', "unknown attribute"),
        ("{.bash exec foo}", "unknown attribute"),
        ("{.bash exec timeout=0}", "timeout must be 1-3600"),
        ("{.bash exec timeout=9999}", "timeout must be 1-3600"),
        ("{.sh exec}", "'sh' names a shell the runner does not know"),
        ("{.python exec}", "not shell"),
        ("{.bash .python exec}", "exactly one language class"),
    ],
)
def test_malformed_tags_are_refused(info, message):
    text = page(f"{FENCE}{info}\necho hi\n{FENCE}")
    assert message in refused(text)


def test_an_expectation_after_a_skip_or_a_json_block_is_refused():
    skip = page(
        f'{FENCE}{{.bash skip reason="dev: x"}}\necho\n{FENCE}\n<!-- expect: y -->'
    )
    json_ = page(f"{FENCE}json\n{{}}\n{FENCE}\n<!-- expect: y -->")
    assert "does not run" in refused(skip)
    assert "does not run" in refused(json_)


def test_an_unclosed_fence_is_refused():
    with pytest.raises(dx.Refused, match="unclosed fence"):
        dx.parse_fences(f"text\n\n{FENCE}python\nprint(1)\n", "p.md")


def test_a_fence_nested_in_a_list_is_still_a_shell_block():
    text = "1. Start it:\n\n    ```bash\n    echo hi\n    ```\n"
    assert "untagged shell fence" in refused(text)


def test_a_four_backtick_fence_holds_examples_as_text():
    text = page(f"````markdown\n{FENCE}bash exec\necho hi\n{FENCE}\n````")
    assert blocks(text) == []


# ---------------------------------------------------------------------------
# What may be in a block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        'curl -s localhost:8000/health/ready | jq .status  # "healthy"',
        "# a whole-line comment",
        "echo hi; # after a semicolon",
    ],
)
def test_a_shell_comment_is_refused(line):
    text = page(f"{FENCE}{{.bash exec}}\n{line}\n{FENCE}")
    assert "shell comment" in refused(text)


@pytest.mark.parametrize(
    "line",
    [
        "curl http://localhost:8000/docs#/flags",
        "echo a#b",
        "echo $#",
        "echo ${#PATH}",
        '[[ "$x" == *#* ]] && echo match',
        """curl -d '{"colour":"#00ff00"}' localhost:8000""",
        'echo "# not a comment"',
    ],
)
def test_a_hash_that_is_not_a_comment_is_allowed(line):
    assert dx.comment_lines(line) == []


def test_a_here_document_body_is_data():
    body = "cat <<'EOF' > config.yml\n# PORT is the listen port\nport: 8000  # default\nEOF\n"
    assert dx.comment_lines(body) == []
    assert dx.comment_lines(body + "echo done  # after\n") == [4]


@pytest.mark.parametrize(
    "body, message",
    [
        ("export TOKEN=$(false)", "masks the substitution"),
        ("local x=$(false)", "masks the substitution"),
        ("echo a \\", "ends in '\\'"),
        ('echo "unterminated', "bash -n"),
        ("if true; then", "bash -n"),
        ("cat <<EOF\nhello", "bash -n"),
        ("unset COMPOSE_PROJECT_NAME; docker compose --dry-run down -v", "COMPOSE"),
        (
            "COMPOSE_PROJECT_NAME=controlvar docker compose down -v",
            "COMPOSE_PROJECT_NAME",
        ),
        ("docker compose -p controlvar down -v", "names a compose project"),
        (
            "docker compose --project-name=controlvar down -v",
            "names a compose project",
        ),
        ("env -u HOME docker compose down", "rewrites the environment"),
        ("env -i docker compose down", "rewrites the environment"),
        ("docker volume rm controlvar_postgres_data", "removes Docker resources"),
        ("docker system prune -f", "removes Docker resources"),
        ("docker rm -f some-container", "removes containers"),
    ],
)
def test_blocks_that_hide_a_failure_or_escape_the_project_are_refused(body, message):
    text = page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}")
    assert message in refused(text)


def test_the_quick_starts_own_down_is_allowed():
    text = page(f"{FENCE}{{.bash exec}}\ndocker compose down -v\n{FENCE}")
    (block,) = blocks(text)
    dx.lint_block(block, "p.md")  # does not raise


def test_an_empty_run_block_is_refused():
    assert "empty block" in refused(page(f"{FENCE}{{.bash exec}}\n\n{FENCE}"))


# ---------------------------------------------------------------------------
# Enrolment and counts
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A scratch repository root with one enrolled page, and no pending list."""
    monkeypatch.setattr(dx, "ROOT", tmp_path)
    monkeypatch.setattr(dx, "ENROLMENT", tmp_path / "scripts" / "doc_examples.toml")
    monkeypatch.setattr(dx, "PENDING_FROZEN", {}, raising=False)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "p.md").write_text(
        page(
            f"{FENCE}{{.bash exec}}\necho hi\n{FENCE}\n<!-- expect: hi -->",
            f'{FENCE}{{.bash skip reason="dev: x"}}\necho no\n{FENCE}',
        )
    )
    return tmp_path


def write(root: pathlib.Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def enrol(root, body: str) -> pathlib.Path:
    path = root / "scripts" / "doc_examples.toml"
    path.write_text(textwrap.dedent(body))
    return path


def problems_of(path: pathlib.Path) -> list[str]:
    with pytest.raises(dx.Refused) as excinfo:
        dx.check(path)
    return excinfo.value.problems


GOOD = """
    [meta]
    documents = 1
    exec = 1
    universe = ["docs/p.md"]

    [[document]]
    path = "docs/p.md"
    environment = "bare"
    exec = 1
    skip = 1
    expects = 1
"""


def test_exact_counts_pass(repo, capsys):
    counts = dx.check(enrol(repo, GOOD))
    assert counts == {"docs/p.md": {"exec": 1, "skip": 1, "expects": 1}}
    assert (
        "universe: 1 pages; 1 enrolled, 0 exempt, 0 pending" in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "old, new, message",
    [
        (
            "expects = 1\n",
            "expects = 2\n",
            "docs/p.md: found 1 expectations; the enrolment says 2. If you added or "
            "removed one on purpose, set expects = 1.",
        ),
        (
            "    exec = 1\n    skip",
            "    exec = 0\n    skip",
            "docs/p.md: found 1 exec blocks; the enrolment says 0.",
        ),
        (
            "documents = 1",
            "documents = 2",
            "scripts/doc_examples.toml lists 1 documents but [meta] documents = 2; "
            "set it to 1.",
        ),
        (
            "exec = 1\n    universe",
            "exec = 2\n    universe",
            "the documents' exec counts add up to 1 but [meta] exec = 2; set it to 1.",
        ),
        ('environment = "bare"', 'environment = "local"', "'local' is not implemented"),
        ('environment = "bare"', 'environment = "cloud"', "is not one of"),
        (
            'path = "docs/p.md"',
            'path = "docs/missing.md"',
            "docs/missing.md is enrolled but does not exist. Renamed? Change the path.",
        ),
        ('path = "docs/p.md"', 'path = "../p.md"', "is not a markdown file"),
    ],
)
def test_enrolment_mismatches_are_refused(repo, old, new, message):
    body = GOOD.replace(old, new, 1)
    assert body != GOOD
    assert any(message in p for p in problems_of(enrol(repo, body))), message


def test_working_instructions_cannot_be_enrolled(repo):
    (repo / "CLAUDE.md").write_text("# x\n")
    assert any(
        "working instructions" in p
        for p in problems_of(
            enrol(repo, GOOD.replace('path = "docs/p.md"', 'path = "CLAUDE.md"'))
        )
    )


def test_a_stack_page_needs_exactly_one_stack_start(repo):
    qs = repo / "docs" / "getting-started"
    qs.mkdir()
    one = f"{FENCE}{{.bash exec}}\ndocker compose up -d --wait\n{FENCE}"
    two = one + "\n\n" + one
    body = textwrap.dedent(
        """
        [meta]
        documents = 2
        exec = {total}
        universe = ["docs/getting-started/quick-start.md", "docs/p.md"]

        [[document]]
        path = "docs/p.md"
        environment = "stack"
        exec = 1
        skip = 1
        expects = 1

        [[document]]
        path = "docs/getting-started/quick-start.md"
        environment = "bare"
        exec = {n}
        skip = 0
        expects = 0
        """
    )
    (qs / "quick-start.md").write_text(page(one))
    dx.check(enrol(repo, body.format(n=1, total=2)))
    (qs / "quick-start.md").write_text(page(two))
    assert any(
        "2 exec blocks equal" in p
        for p in problems_of(enrol(repo, body.format(n=2, total=3)))
    )


# ---------------------------------------------------------------------------
# The universe: every page the walk finds is under the contract
# ---------------------------------------------------------------------------


def test_the_walk_prunes_by_name_at_any_depth(tmp_path):
    for rel in (
        "README.md",
        "docs/a.md",
        ".github/pull_request_template.md",
        "frontend/node_modules/pkg/README.md",
        "sdk/js/node_modules/x/y/README.md",
        "node_modules/z.md",
        "venv/lib/x.md",
        "demo/app/venv/x.md",
        "site/index.md",
        "docs/site/built.md",
        ".claude/worktrees/w/docs/a.md",
        ".pytest_cache/README.md",
        "frontend/.next/x.md",
        "CLAUDE.md",
        "backend/CLAUDE.md",
        "notes.txt",
    ):
        write(tmp_path, rel, "# x\n")
    assert dx.walk(tmp_path) == {
        "README.md",
        "docs/a.md",
        ".github/pull_request_template.md",
    }


def test_a_new_page_the_universe_does_not_list_is_refused(repo):
    write(repo, "docs/guides/new.md", "# New\n\nNo code at all.\n")
    assert problems_of(enrol(repo, GOOD)) == [
        "docs/guides/new.md: a page scripts/doc_examples.toml does not know. Add it "
        "to [meta] universe (docs/development/doc-examples.md#enrolling-a-page)."
    ]


def test_a_listed_page_the_walk_does_not_find_is_refused(repo):
    body = GOOD.replace(
        'universe = ["docs/p.md"]', 'universe = ["docs/gone.md", "docs/p.md"]'
    )
    assert problems_of(enrol(repo, body)) == [
        "docs/gone.md is in [meta] universe but the walk does not find it. Renamed? "
        "Change the path. Deleted? Remove it."
    ]


def test_a_page_in_a_pruned_directory_cannot_be_enrolled(repo):
    write(repo, "docs/site/p.md", (repo / "docs" / "p.md").read_text())
    body = GOOD.replace('path = "docs/p.md"', 'path = "docs/site/p.md"')
    found = problems_of(enrol(repo, body))
    assert any(
        "docs/site/p.md is listed but is outside the universe" in p for p in found
    )


@pytest.mark.regression
def test_a_document_dropped_with_its_count_is_refused(repo):
    """R-E1 (E-T1): deleting an entry together with its count used to pass."""
    write(
        repo,
        "docs/q.md",
        page(f"{FENCE}{{.bash exec}}\necho q\n{FENCE}\n<!-- expect: q -->"),
    )
    both = textwrap.dedent(GOOD).replace(
        'universe = ["docs/p.md"]', 'universe = ["docs/p.md", "docs/q.md"]'
    ).replace("documents = 1", "documents = 2").replace(
        "exec = 1\nuniverse", "exec = 2\nuniverse"
    ) + textwrap.dedent(
        """
        [[document]]
        path = "docs/q.md"
        environment = "bare"
        exec = 1
        skip = 0
        expects = 1
        """
    )
    dx.check(enrol(repo, both))
    dropped = both.split('[[document]]\npath = "docs/q.md"')[0]
    dropped = dropped.replace("documents = 2", "documents = 1").replace(
        "exec = 2\nuniverse", "exec = 1\nuniverse"
    )
    assert problems_of(enrol(repo, dropped)) == [
        "docs/q.md: 1 shell block, and the page is not enrolled. Add it to "
        "scripts/doc_examples.toml (docs/development/doc-examples.md#enrolling-a-page), "
        "or list it as exempt with a reason."
    ]


@pytest.mark.regression
def test_a_zero_fence_document_cannot_hold_a_place(repo):
    """R-E2 (E-T1b): swapping a real page for one with no shell block kept the count."""
    write(repo, "docs/empty.md", "# Empty\n\n```json\n{}\n```\n")
    body = GOOD.replace(
        'universe = ["docs/p.md"]', 'universe = ["docs/empty.md", "docs/p.md"]'
    )
    swapped = (
        body.replace('path = "docs/p.md"', 'path = "docs/empty.md"')
        .replace(
            "exec = 1\n    skip = 1\n    expects = 1",
            "exec = 0\n    skip = 0\n    expects = 0",
        )
        .replace("exec = 1\n    universe", "exec = 0\n    universe")
    )
    found = problems_of(enrol(repo, swapped))
    assert (
        "docs/empty.md is listed as enrolled but has no shell blocks; remove the entry "
        "and set [meta] documents to match."
    ) in found
    assert any(
        p.startswith("docs/p.md: 2 shell blocks, and the page is not enrolled")
        for p in found
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "language",
    [
        "zsh",
        "sh-session",
        "shell-session",
        "console",
        "shell",
        "sh",
        "fish",
        "powershell",
        "ps1",
        "Bash",
    ],
)
@pytest.mark.parametrize("enrolled", [True, False], ids=["enrolled", "unlisted"])
def test_a_shell_fence_in_an_unknown_shell_language_is_refused(
    repo, language, enrolled
):
    """R-E3 (E-T6): a block retagged ```zsh left the check, counts and all."""
    target = "docs/p.md" if enrolled else "docs/other.md"
    if not enrolled:
        write(repo, target, "# Other\n")
    body = (
        GOOD
        if enrolled
        else GOOD.replace(
            'universe = ["docs/p.md"]', 'universe = ["docs/other.md", "docs/p.md"]'
        )
    )
    path = repo / target
    lines = path.read_text().split("\n")
    lines += ["", f"{FENCE}{language}", "echo retagged", FENCE, ""]
    path.write_text("\n".join(lines))
    line = len(lines) - 3
    assert (
        f"{target}:{line}: '{language}' names a shell the runner does not know; use {{.bash …}}"
        in problems_of(enrol(repo, body))
    )


def test_a_fence_that_names_no_language_is_refused_on_every_page(repo):
    write(repo, "README.md", "# Readme\n\n```\nsome output\n```\n")
    body = GOOD.replace(
        'universe = ["docs/p.md"]', 'universe = ["README.md", "docs/p.md"]'
    )
    assert problems_of(enrol(repo, body)) == [
        "README.md:3: the fence names no language; name one (`text` for output) "
        "(docs/development/doc-examples.md#tagging-a-shell-block)"
    ]


def test_the_lists_are_disjoint(repo):
    body = GOOD + textwrap.dedent(
        """
        [[exempt]]
        path = "docs/p.md"
        reason = "x"
        """
    )
    assert any(
        "docs/p.md is listed 2 times (enrolled, exempt)" in p
        for p in problems_of(enrol(repo, body))
    )


def test_an_exempt_page_needs_a_reason_and_a_shell_block_and_obeys_the_rules(repo):
    write(
        repo,
        "docs/ops.md",
        "# Ops\n\n```bash\naws s3 ls  # lists\n```\n\n```bash\necho 'x\n```\n",
    )
    write(repo, "docs/none.md", "# None\n")
    body = GOOD.replace(
        'universe = ["docs/p.md"]',
        'universe = ["docs/none.md", "docs/ops.md", "docs/p.md"]',
    ) + textwrap.dedent(
        """
        [[exempt]]
        path = "docs/ops.md"
        reason = "operator runbook, run by hand"

        [[exempt]]
        path = "docs/none.md"
        reason = "x"
        """
    )
    found = problems_of(enrol(repo, body))
    assert (
        "docs/none.md is listed as exempt but has no shell blocks; remove the entry."
        in found
    )
    assert any(p.startswith('docs/ops.md:4: shell comment "# lists"') for p in found)
    assert any(p.startswith("docs/ops.md:7: syntax (bash -n)") for p in found)
    assert len(found) == 3, found
    assert any(
        "exempt needs a reason" in p
        for p in problems_of(enrol(repo, body.replace('reason = "x"', 'reason = " "')))
    )


# ---------------------------------------------------------------------------
# Pending: the rollout list, frozen in code, only shrinks
# ---------------------------------------------------------------------------

PENDING_PAGE = (
    "# Ops\n\n```bash\naws s3 ls  # lists the buckets\n```\n\n```\nout\n```\n"
)
WITH_PENDING = (
    GOOD.replace('universe = ["docs/p.md"]', 'universe = ["docs/ops.md", "docs/p.md"]')
    + """
    [pending]
    "docs/ops.md" = { comments = 1, unlabelled = 1, syntax = 0 }
"""
)


@pytest.fixture
def pending_repo(repo, monkeypatch):
    write(repo, "docs/ops.md", PENDING_PAGE)
    monkeypatch.setattr(dx, "PENDING_FROZEN", {"docs/ops.md": (1, 1, 0)})
    return repo


def test_a_pending_page_carries_its_exact_residual(pending_repo, capsys):
    dx.check(enrol(pending_repo, WITH_PENDING))
    assert (
        "pending residual: 1 comment lines, 1 unlabelled fences, 0 bash -n failures"
        in capsys.readouterr().out
    )
    found = problems_of(
        enrol(pending_repo, WITH_PENDING.replace("comments = 1", "comments = 0"))
    )
    assert found == [
        "docs/ops.md: found 1 shell comment lines; [pending] says 0. Set comments = 1."
    ]


def test_pending_cannot_grow(pending_repo):
    write(pending_repo, "docs/new.md", "# New\n\n```bash\necho new\n```\n")
    body = (
        WITH_PENDING.replace(
            '"docs/ops.md", "docs/p.md"]', '"docs/new.md", "docs/ops.md", "docs/p.md"]'
        )
        + '    "docs/new.md" = { comments = 0, unlabelled = 0, syntax = 0 }\n'
    )
    found = problems_of(enrol(pending_repo, body))
    assert len(found) == 1 and found[0].startswith(
        "docs/new.md: pending cannot grow, and this page was not pending when the list "
        "was frozen (PENDING_FROZEN in scripts/doc_examples.py)."
    ), found


def test_a_pending_residual_can_only_fall(pending_repo):
    write(
        pending_repo,
        "docs/ops.md",
        PENDING_PAGE.replace("aws s3 ls", "# more\naws s3 ls"),
    )
    found = problems_of(
        enrol(pending_repo, WITH_PENDING.replace("comments = 1", "comments = 2"))
    )
    assert found == [
        "docs/ops.md: 2 shell comment lines, more than the 1 this page had when pending "
        "was frozen; the residual can only fall."
    ]
    write(
        pending_repo, "docs/ops.md", PENDING_PAGE.replace("  # lists the buckets", "")
    )
    dx.check(enrol(pending_repo, WITH_PENDING.replace("comments = 1", "comments = 0")))


def test_a_pending_page_with_nothing_pending_must_leave(pending_repo):
    write(pending_repo, "docs/ops.md", "# Ops\n\n```text\nout\n```\n")
    found = problems_of(
        enrol(
            pending_repo,
            WITH_PENDING.replace(
                "comments = 1, unlabelled = 1", "comments = 0, unlabelled = 0"
            ),
        )
    )
    assert found == [
        "docs/ops.md is pending but has nothing pending (no shell blocks, and every fence "
        "names a language); remove its [pending] entry."
    ]


def test_an_exec_tag_on_a_pending_page_is_refused(pending_repo):
    write(
        pending_repo, "docs/ops.md", PENDING_PAGE + "\n```{.bash exec}\necho hi\n```\n"
    )
    assert any(
        "docs/ops.md:11: an exec block on a pending page never runs" in p
        for p in problems_of(enrol(pending_repo, WITH_PENDING))
    )


def test_the_frozen_pending_list_is_the_enrolment_s_pending_list_or_larger():
    """The TOML may only drop pages the frozen list holds, never add one."""
    pending = dx.load().pending
    assert set(pending) <= set(dx.PENDING_FROZEN)


# ---------------------------------------------------------------------------
# Skip reasons, package managers, AWS, bash -n on skip blocks
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "reason, ok",
    [
        ("x", False),
        ("because it needs AWS", False),
        ("aws needs an account", False),
        ("aws: needs an account", True),
        ("AWS: needs an account", False),
        ("cloud: needs an account", False),
        ("aws:", False),
        ("aws: ", False),
        ("bug #123: the endpoint returns 500", True),
        ("bug: the endpoint returns 500", False),
        ("bug #abc: x", False),
        ("bug#1: x", False),
        ("dev #1: x", False),
        ("fragment: fill in your values", True),
    ],
)
def test_skip_reason_names_a_category(reason, ok):
    """R-E7: `reason="x"` said nothing about why a block could not run."""
    text = page(f'{FENCE}{{.bash skip reason="{reason}"}}\necho hi\n{FENCE}')
    if ok:
        (block,) = blocks(text)
        assert dx.category_of(block.reason) == reason.split(":")[0].split(" ")[0]
    else:
        assert (
            "skip reason must start with one of: checkout, dev, server, aws"
            in refused(text)
        )


@pytest.mark.parametrize(
    "body, tool",
    [
        ("pip install openfeature-sdk experimently experimently-openfeature", "pip"),
        ("gem install experimently", "gem"),
        ("npx @getexperimently/cli init", "npx"),
        ("uv pip install experimently", "uv"),
        ("cd sdk/js && npm ci", "npm"),
        ("sudo pip3 install experimently", "pip3"),
        ("python -m pip install experimently", "python -m pip"),
        ("dotnet add package Experimently.SDK", "dotnet add"),
        ("composer require experimently/sdk", "composer"),
        ("go get github.com/getexperimently/go-sdk", "go get"),
        ("FOO=1 yarn add @getexperimently/js-sdk", "yarn"),
        ("VERSION=$(npm view x version)", "npm"),
        ("if true; then pnpm i; fi", "pnpm"),
        ("pipx run experimently", "pipx"),
        ("poetry add experimently", "poetry"),
    ],
)
def test_a_block_that_runs_may_not_install_or_run_a_package(body, tool):
    """M6 (T29): until the names are published, a run would fetch a squatter's."""
    text = page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}")
    assert f"may not run '{tool}'" in refused(text)


@pytest.mark.parametrize(
    "body",
    [
        "aws sts get-caller-identity",
        "cdk deploy --all",
        "sam build",
        "cd infrastructure && cdk synth",
        "ARN=$(aws sts get-caller-identity --query Arn)",
    ],
)
def test_a_block_that_runs_may_not_reach_aws(body):
    """PE C10: an exec block never reaches an AWS account."""
    text = page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}")
    assert "may not call '" in refused(text)


@pytest.mark.parametrize(
    "body",
    [
        "echo npm pip aws",
        "curl -s https://registry.npmjs.org/react | jq .name",
        'echo "run npm ci"',
        "ls node_modules",
        "echo sam-and-cdk",
    ],
)
def test_the_words_alone_are_not_a_package_manager_or_aws(body):
    (block,) = blocks(page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}"))
    assert dx.block_problems(block, "p.md") == []


def test_a_registry_skip_must_run_a_package_manager():
    good = page(
        f'{FENCE}{{.bash skip reason="registry: not published"}}\npip install experimently\n{FENCE}'
    )
    bad = page(
        f'{FENCE}{{.bash skip reason="registry: not published"}}\ncurl localhost\n{FENCE}'
    )
    (block,) = blocks(good)
    assert dx.block_problems(block, "p.md") == []
    assert (
        "the skip reason says 'registry' but the block runs no package manager"
        in refused(bad)
    )


def test_bash_n_runs_on_skip_blocks_too_except_fragments():
    broken = "aws s3 cp 'x"
    skip = page(
        f'{FENCE}{{.bash skip reason="aws: needs an account"}}\n{broken}\n{FENCE}'
    )
    fragment = page(
        f'{FENCE}{{.bash skip reason="fragment: fill it in"}}\n{broken}\n{FENCE}'
    )
    assert "syntax (bash -n)" in refused(skip)
    (block,) = blocks(fragment)
    assert dx.block_problems(block, "p.md") == []


def test_bash_n_is_refused_with_a_bash_older_than_4_4(tmp_path, monkeypatch):
    """PE C16: macOS's /bin/bash 3.2 would give different answers than CI."""
    fake = tmp_path / "bash"
    fake.write_text("#!/bin/sh\necho 3.2\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{__import__('os').pathsep}/usr/bin:/bin")
    monkeypatch.setattr(dx, "_SYNTAX_BASH", [])
    with pytest.raises(dx.Refused, match="is bash 3.2; the examples need bash >= 4.4"):
        dx.syntax_problem("echo hi")


def test_the_aws_cli_cannot_read_the_callers_credentials():
    env = dx.scrubbed_env("docex-unit", {})
    assert (
        env["AWS_CONFIG_FILE"]
        == env["AWS_SHARED_CREDENTIALS_FILE"]
        == __import__("os").devnull
    )
    problems = _run_page(
        'echo "$AWS_SHARED_CREDENTIALS_FILE $AWS_CONFIG_FILE"',
        expects={0: [f"{__import__('os').devnull} {__import__('os').devnull}"]},
    )
    assert problems == [], problems


# ---------------------------------------------------------------------------
# Messages: every problem in one run, one prefix, annotations in CI
# ---------------------------------------------------------------------------


def test_every_problem_is_reported_in_one_run(repo, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    write(
        repo,
        "docs/p.md",
        page(
            f"{FENCE}{{.bash exec}}\necho a  # one\necho b  # two\n{FENCE}\n<!-- expect: a -->",
            f'{FENCE}{{.bash skip reason="x"}}\necho no  # three\n{FENCE}',
        ),
    )
    enrol(repo, GOOD)
    assert dx.main(["--check"]) == 2
    err = capsys.readouterr().err.strip().split("\n")
    assert err[-1] == "4 problems"
    refusals = err[:-1]
    assert len(refusals) == 4, err
    assert all(
        line.count("refused:") == 1 and line.startswith("refused: ")
        for line in refusals
    )
    assert sum('shell comment "#' in line for line in refusals) == 2
    assert sum("skip reason must start" in line for line in refusals) == 1


def test_problems_are_annotated_in_github_actions(repo, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    write(
        repo,
        "docs/p.md",
        (repo / "docs" / "p.md").read_text().replace("echo no", "echo no  # a, b: c"),
    )
    enrol(repo, GOOD)
    assert dx.main(["--check"]) == 2
    err = capsys.readouterr().err
    assert (
        '::error file=docs/p.md,line=9::docs/p.md:9: shell comment "# a, b: c" breaks '
        "when pasted into zsh." in err
    ), err


def test_the_authoring_pages_sample_is_what_the_check_prints(repo, monkeypatch, capsys):
    """The 'When the check fails' sample is real output, not a paraphrase."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    authoring = (REPO / "docs" / "development" / "doc-examples.md").read_text()
    section = authoring.split("## When the check fails", 1)[1]
    sample = section.split("```text\n", 1)[1].split("\n```", 1)[0]
    (repo / "docs" / "p.md").unlink()
    write(
        repo,
        "docs/guides/new-page.md",
        "# New page\n\nStart it:\n\n```bash\necho start\n```\n\n```zsh\necho z\n```\n",
    )
    write(
        repo,
        "docs/guides/tour.md",
        "# Tour\n\nCreate a flag and read back its id. The block below prints it.\n\n"
        + "\n" * 8
        + "```{.bash exec}\n"
        + "curl -s localhost:8000/api/v1/feature-flags/ | jq -r '.[0].id'  # prints the flag's id\n"
        + "```\n<!-- expect: - -->\n",
    )
    enrol(
        repo,
        """
        [meta]
        documents = 1
        exec = 1
        universe = ["docs/guides/tour.md"]

        [[document]]
        path = "docs/guides/tour.md"
        environment = "bare"
        exec = 1
        skip = 0
        expects = 1
        """,
    )
    assert dx.main(["--check"]) == 2
    assert capsys.readouterr().err.strip() == sample.strip()


# ---------------------------------------------------------------------------
# Output a CI log can use: measured counts, line by line
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_run_counts_the_blocks_it_executed():
    """R-E4: `passed (N exec)` printed the enrolment's number, not what ran."""
    text = page(
        f"{FENCE}{{.bash exec}}\necho a\n{FENCE}\n<!-- expect: a -->",
        f"{FENCE}{{.bash exec}}\necho b\n{FENCE}\n<!-- expect: b -->",
    )
    problems, reached = dx.execute_counted(
        blocks(text), "p.md", dx.scrubbed_env("docex-unit", {})
    )
    assert (problems, reached) == ([], 2)
    stops = page(
        f"{FENCE}{{.bash exec}}\necho a\n{FENCE}\n<!-- expect: a -->",
        f"{FENCE}{{.bash exec}}\nexit 0\n{FENCE}",
        f"{FENCE}{{.bash exec}}\necho c\n{FENCE}\n<!-- expect: c -->",
    )
    _, stopped = dx.execute_counted(
        blocks(stops), "p.md", dx.scrubbed_env("docex-unit", {})
    )
    assert stopped == 1
    doc = {"path": "p.md", "exec": 3}
    line, found = dx.run_verdict(doc, problems, reached)
    assert line == "p.md: FAILED (2/3 exec blocks ran)"
    assert found == ["p.md: 2 exec blocks reached their end; the enrolment says 3"]
    assert dx.run_verdict({"path": "p.md", "exec": 2}, [], 2) == (
        "p.md: passed (2/2 exec blocks ran)",
        [],
    )


@pytest.mark.regression
def test_the_runner_writes_its_output_line_by_line():
    """R-E5 (the runner's half): a pipe is block-buffered, so a job killed mid-run
    lost the name of the page it was on.  The deadline half is E0b's."""
    import subprocess

    probe = textwrap.dedent(
        f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("dx", {str(REPO / "scripts" / "doc_examples.py")!r})
        dx = importlib.util.module_from_spec(spec)
        sys.modules["dx"] = dx
        spec.loader.exec_module(dx)
        try:
            dx.main([])
        except SystemExit:
            pass
        print(sys.stdout.line_buffering, sys.stderr.line_buffering)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.stdout.strip() == "True True", result.stderr


# ---------------------------------------------------------------------------
# Rendering, through the site's own Markdown extensions
# ---------------------------------------------------------------------------


def _render(text: str) -> str:
    import markdown

    config = yaml.safe_load((REPO / "mkdocs.yml").read_text())
    names, options = [], {}
    for entry in config["markdown_extensions"]:
        if isinstance(entry, dict):
            ((name, opts),) = entry.items()
            names.append(name)
            if opts:
                options[name] = opts
        else:
            names.append(entry)
    options.pop("pymdownx.snippets", None)
    names = [n for n in names if n != "pymdownx.snippets"]
    return markdown.markdown(text, extensions=names, extension_configs=options)


def _problems(text: str) -> list[str]:
    return dx.render_problems(text, _render(text), "p.md")


def test_good_tags_render_as_code():
    text = page(
        f"{FENCE}{{.bash exec timeout=30}}\necho hi\n{FENCE}\n<!-- expect: hi -->",
        f'{FENCE}{{.bash skip reason="dev: x"}}\necho no\n{FENCE}',
    )
    assert _problems(text) == []
    assert "expect" not in _render(text).replace("<!-- expect: hi -->", "")


@pytest.mark.parametrize(
    "text",
    [
        page(
            f"Intro.\n\n{FENCE}bash exec\necho destroyed\n{FENCE}"
        ),  # last on the page
        page(
            f"{FENCE}bash exec\necho destroyed\n{FENCE}",
            f"{FENCE}bash\necho next\n{FENCE}",
        ),  # swallows the next fence
        page(f'{FENCE}{{.bash skip reason="dev: a"}} trailing\necho x\n{FENCE}'),
    ],
    ids=["destroyed-last", "destroyed-then-fence", "trailing-text"],
)
def test_a_destroyed_fence_is_caught_by_the_render_check(text):
    assert _problems(text)


def test_no_dot_renders_as_code_so_only_the_source_check_sees_it():
    text = page(f"{FENCE}{{bash exec}}\necho hi\n{FENCE}")
    assert _problems(text) == []
    assert "without '.'" in refused(text)


def test_inline_code_that_mentions_a_fence_is_not_a_leak():
    text = page(
        "Write `` ```{.bash exec} `` for a block that runs, or\n"
        '`` ```{.bash skip reason="..."} `` for one that does not.',
        "Pass `extra={...}` to the logger.",
        "`docker compose\nup -d` starts it.",
    )
    assert _problems(text) == []


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


def test_the_repository_enrolment_holds():
    dx.check()


def test_the_site_toolchain_is_installed_from_one_place():
    """The page the render check inspects is built by exactly what ships."""
    for workflow in ("docs.yml", "doc-examples.yml"):
        text = (REPO / ".github" / "workflows" / workflow).read_text()
        assert "bash scripts/docs_toolchain.sh" in text, workflow
        assert "grep -E '^mkdocs" not in text, f"{workflow} spells the install itself"


# ---------------------------------------------------------------------------
# Execution (no Docker: a page's blocks, one bash, sentinels between them)
# ---------------------------------------------------------------------------


def _run_page(*bodies: str, expects=None, extra_env=None) -> list[str]:
    """Run fixture blocks through execute(); return the problems it reports."""
    expects = expects or {}
    parts = []
    for i, body in enumerate(bodies):
        lines = "".join(f"\n<!-- expect: {e} -->" for e in expects.get(i, []))
        parts.append(f"{FENCE}{{.bash exec timeout=10}}\n{body}\n{FENCE}{lines}")
    page_blocks = blocks(page(*parts))
    env = dx.scrubbed_env("docex-unit", {})
    env.update(extra_env or {})
    return dx.execute(page_blocks, "p.md", env)


def test_a_clean_page_passes():
    assert (
        _run_page("echo one", "X=two\necho $X", expects={0: ["one"], 1: ["two"]}) == []
    )


def test_variables_carry_from_block_to_block():
    assert _run_page("TOKEN=abc", 'echo "$TOKEN"', expects={1: ["abc"]}) == []


@pytest.mark.parametrize(
    "body, message",
    [
        ("cat\necho after", ""),  # stdin is /dev/null: cat reads EOF, the page goes on
        ("false && true", "block exited 1"),
        ("set +e\necho x", "turned off strict mode"),
        ("exit 0", "did not reach its end"),
        ("false\necho unreachable", "did not reach its end"),
        ("yes | head -1", "did not reach its end"),  # 141 under pipefail: loud
    ],
    ids=["cat", "false-and-true", "set-plus-e", "exit-0", "false", "sigpipe"],
)
def test_the_shapes_that_used_to_pass_silently(body, message):
    problems = _run_page(body, "echo after", expects={0: ["after"], 1: ["after"]})
    if message:
        assert problems and message in problems[0], problems
    else:
        assert problems == [], problems


def test_an_exit_0_is_counted_by_the_blocks_it_skipped():
    problems = _run_page("echo a", "exit 0", "echo c", expects={0: ["a"], 2: ["c"]})
    assert any("executed 1/3 exec blocks" in p for p in problems), problems


def test_the_exit_status_is_read_before_anything_resets_it():
    # `false && true` leaves $? at 1 without tripping errexit; the sentinel must see 1.
    assert "block exited 1" in _run_page("false && true")[0]


def test_expectations_are_word_bounded_and_in_order():
    assert dx.find_expectation('"status": "inactive"', '"active"') == -1
    assert dx.find_expectation("id 1c9e4015-4401b", "401") == -1
    assert (
        dx.find_expectation('"total_conversions": 10,', '"total_conversions": 1') == -1
    )
    assert dx.find_expectation('"status": "active"', '"active"') != -1
    problems = _run_page("echo second; echo first", expects={0: ["first", "second"]})
    assert problems and "expectation 2/2 not found" in problems[0]


def test_expectations_are_matched_on_stdout_only():
    problems = _run_page("echo only-on-stderr >&2", expects={0: ["only-on-stderr"]})
    assert problems and "not found" in problems[0]


def test_output_without_an_expectation_fails():
    problems = _run_page("echo something")
    assert problems and "no expectation" in problems[0]


def test_the_environment_is_scrubbed(monkeypatch):
    monkeypatch.setenv("TOKEN", "leaked-from-the-caller")
    assert "TOKEN" not in dx.scrubbed_env("docex-unit", {})
    problems = _run_page('echo "${TOKEN-unset}"', expects={0: ["unset"]})
    assert problems == [], problems


def test_the_block_text_is_what_runs():
    body = "echo 'a  b'\nlocalhost=1; echo $localhost"
    (block,) = blocks(page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}"))
    script = dx.write_script([block], "n0nce")
    assert body + "\n" in script


def test_a_sentinel_cannot_be_forged():
    problems = _run_page(
        "printf '@@DOCEX:guess:END:0:0:ehuB:pf@@\\n'; false",
    )
    assert problems and "did not reach its end" in problems[0]


def test_a_timeout_kills_the_group_and_names_the_block(tmp_path):
    pidfile = tmp_path / "pid"
    text = page(
        f"{FENCE}{{.bash exec timeout=1}}\nsleep 300 & echo $! > {pidfile}; wait\n{FENCE}"
    )
    problems = dx.execute(blocks(text), "p.md", dx.scrubbed_env("docex-unit", {}))
    assert problems and "timed out after 1s; process group killed" in "\n".join(
        problems
    )
    background = int(pidfile.read_text())
    assert _dies(background), f"the background sleep {background} is still running"


def _gone_or_zombie(pid: int) -> bool:
    """True once ``pid`` has exited. A process killed after its parent died is
    a zombie until whoever inherited it reaps it, and ``kill(pid, 0)`` still
    succeeds on a zombie, so its state is read as well. Only ``kill`` saying
    there is no such process, or ``ps`` saying Z, counts: an empty or failed
    ``ps`` does not, and the caller asks again."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return state.startswith("Z")


def _dies(pid: int, within: float = 10.0) -> bool:
    """Wait up to ``within`` seconds for ``pid`` to exit: SIGKILL is delivered
    asynchronously. A process that survived the kill never satisfies this."""
    import time

    deadline = time.monotonic() + within
    while not _gone_or_zombie(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_a_killed_process_nobody_has_reaped_counts_as_gone():
    """The two kill tests once asked only ``kill(pid, 0)``, which succeeds on a
    zombie: core-build failed with "DID NOT RAISE ProcessLookupError" whenever
    the orphaned sleep had not been reaped yet. Here the zombie is made on
    purpose (killed, not yet waited for) and must count as gone."""
    child = subprocess.Popen(["sleep", "300"])
    try:
        child.kill()
        assert _dies(child.pid)
        os.kill(child.pid, 0)  # still a zombie: the old check fails here
    finally:
        child.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)


def test_a_process_that_is_still_running_does_not_count_as_gone():
    child = subprocess.Popen(["sleep", "300"])
    try:
        assert not _dies(child.pid, within=0.3)
    finally:
        child.kill()
        child.wait()


def test_the_failure_issue_job_waits_for_render_examples_and_summary_only():
    """Not cache-warm (a cold cache is not a red page); the summary, because a
    shard that never ran is red only there (PE C6)."""
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "doc-examples.yml").read_text()
    )
    needs = workflow["jobs"]["failure-issue"]["needs"]
    assert set(needs) == {"render", "examples", "summary"}


def test_a_timed_out_shard_files_the_issue_too():
    """A job that reaches timeout-minutes concludes `cancelled` (measured on the
    E0b tamper run), which `failure()` alone does not see."""
    condition = yaml.safe_load(
        (REPO / ".github" / "workflows" / "doc-examples.yml").read_text()
    )["jobs"]["failure-issue"]["if"]
    assert condition == (
        "${{ github.event_name != 'pull_request' && !cancelled() && "
        "(failure() || contains(needs.*.result, 'cancelled')) }}"
    )


def test_only_the_stack_start_may_print_without_an_expectation():
    """compose writes the build log to stdout when it builds, nothing when it doesn't."""
    printed = dx.Outcome(
        reached=True, rc=0, flags="ehuB", pipefail="pf", stdout="#1 build\n"
    )
    stack_up = dx.Block(30, "exec", "bash", dx.STACK_UP)
    other = dx.Block(40, "exec", "bash", "docker compose up -d")
    assert dx.judge([stack_up], [printed], None, 0, "p.md") == []
    problems = dx.judge([other], [printed], None, 0, "p.md")
    assert problems and "no expectation" in problems[0]


# ---------------------------------------------------------------------------
# Fix round (code review of #179): bypasses, old bash, indented blocks, .MD
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "body, message",
    [
        ("\\aws sts get-caller-identity", "may not call 'aws'"),
        ("\\pip install experimently", "may not run 'pip'"),
        ("/usr/local/bin/aws sts get-caller-identity", "may not call 'aws'"),
        ("/usr/bin/pip install experimently", "may not run 'pip'"),
        ('"aws" sts get-caller-identity', "may not call 'aws'"),
        ("'npm' ci", "may not run 'npm'"),
        ("sudo /usr/bin/env FOO=1 \\npx x", "may not run 'npx'"),
        ("cd sdk/java && mvn install", "may not run 'mvn'"),
        ("cd sdk/android && ./gradlew build", "may not run 'gradlew'"),
        ("gradle build", "may not run 'gradle'"),
        ("swift package resolve", "may not run 'swift package'"),
        ("swift build", "may not run 'swift build'"),
        ("dotnet restore", "may not run 'dotnet restore'"),
        ("dotnet build", "may not run 'dotnet build'"),
        ("dotnet add package Experimently.SDK", "may not run 'dotnet add'"),
    ],
)
def test_a_command_word_is_normalised_before_it_is_matched(body, message):
    """A backslash, quotes or a path prefix is still the same command."""
    assert message in refused(page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}"))


@pytest.mark.parametrize(
    "body",
    ["echo '/usr/bin/aws'", "ls ./gradlew", "echo swift", 'echo "\\npm"'],
)
def test_normalising_does_not_make_arguments_commands(body):
    (block,) = blocks(page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}"))
    assert dx.block_problems(block, "p.md") == []


def _fake_old_bash(tmp_path, monkeypatch):
    fake = tmp_path / "fakebin" / "bash"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 3.2\n")
    fake.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{fake.parent}{__import__('os').pathsep}/usr/bin{__import__('os').pathsep}/bin",
    )


@pytest.mark.regression
def test_an_old_bash_is_one_problem_among_the_others(repo, monkeypatch, tmp_path):
    """bash < 4.4 used to escape check() and discard every other problem."""
    write(repo, "docs/new.md", "# New\n")
    _fake_old_bash(tmp_path, monkeypatch)
    found = problems_of(enrol(repo, GOOD))
    assert len(found) == 2, found
    assert any("is bash 3.2; the examples need bash >= 4.4" in p for p in found)
    assert any(p.startswith("docs/new.md: a page") for p in found)
    # and the next run resolves bash again rather than silently skipping bash -n
    with pytest.raises(dx.Refused, match="bash 3.2"):
        dx.syntax_problem("echo hi")


def test_an_old_bash_skips_the_pending_syntax_count_rather_than_misreporting_it(
    repo, monkeypatch, tmp_path
):
    write(repo, "docs/ops.md", "# Ops\n\n```bash\necho 'x\n```\n")
    monkeypatch.setattr(dx, "PENDING_FROZEN", {"docs/ops.md": (0, 0, 1)})
    body = (
        GOOD.replace(
            'universe = ["docs/p.md"]', 'universe = ["docs/ops.md", "docs/p.md"]'
        )
        + '\n[pending]\n"docs/ops.md" = { comments = 0, unlabelled = 0, syntax = 1 }\n'
    )
    dx.check(enrol(repo, body))
    _fake_old_bash(tmp_path, monkeypatch)
    found = problems_of(enrol(repo, body))
    assert len(found) == 1 and "bash 3.2" in found[0], found


INDENTED = {
    "plain": ("# P\n\nText.\n\n    curl -s localhost:8000/health\n", [5]),
    "after a heading": ("# P\n    curl x\n", [2]),
    "tab": ("# P\n\ntext\n\n\tcurl x\n", [5]),
    "list continuation": ("# P\n\n- item\n\n    continued paragraph\n", []),
    "numbered continuation": ("# P\n\n1. step\n\n    more text\n", []),
    "code in a list": ("# P\n\n- item\n\n        curl -s x\n", [5]),
    "nested list": ("# P\n\n- a\n\n    - b\n\n        text\n", []),
    "admonition": ("# P\n\n!!! note\n    body text\n\n    more body\n", []),
    "tab container": ('# P\n\n=== "A"\n\n    body text\n', []),
    "lazy paragraph": ("# P\n\ntext\n    still the paragraph\n", []),
    "in a fence": ("# P\n\n```text\n\n    not a block\n```\n", []),
    "fence in a list": ("# P\n\n1. Run:\n\n    ```bash\n    ls\n    ```\n", []),
    "in an html comment": ("# P\n\n<!--\n\n    hidden\n-->\n", []),
    "after the list ends": ("# P\n\n- item\n\nback to text\n\n    curl x\n", [7]),
    "two lines one block": ("# P\n\ntext\n\n    a\n\n    b\n", [5]),
}


@pytest.mark.regression
@pytest.mark.parametrize("name", INDENTED)
def test_indented_code_blocks_are_found_where_the_site_renders_them(name):
    """What the check calls an indented code block is what MkDocs renders as one."""
    text, lines = INDENTED[name]
    assert dx.indented_code_lines(text) == lines
    scan = dx._PageScan()
    scan.feed(_render(text))
    assert scan.code_blocks - len(dx.parse_fences(text, "p.md")) == len(lines)


def test_no_page_renders_code_the_check_cannot_see():
    """Pinned to the renderer on every walked page, so the rule cannot drift."""
    drift = {}
    for rel in sorted(dx.walk(REPO)):
        text = (REPO / rel).read_text(encoding="utf-8")
        scan = dx._PageScan()
        scan.feed(_render(text))
        hidden = scan.code_blocks - len(dx.parse_fences(text, rel))
        if hidden != len(dx.indented_code_lines(text)):
            drift[rel] = (hidden, dx.indented_code_lines(text))
    assert drift == {}


@pytest.mark.regression
def test_an_indented_curl_block_is_refused_on_any_page(repo):
    write(
        repo,
        "README.md",
        "# Readme\n\nCheck it:\n\n    curl -s localhost:8000/health\n",
    )
    body = GOOD.replace(
        'universe = ["docs/p.md"]', 'universe = ["README.md", "docs/p.md"]'
    )
    assert problems_of(enrol(repo, body)) == [
        "README.md:5: an indented code block; use a fenced block with a language "
        "(`text` for output) (docs/development/doc-examples.md#tagging-a-shell-block)"
    ]


# A shell block whose opening fence is missing (#434): its comments render as
# page titles and its commands as a paragraph. The closing fence then opens a
# block of its own, which is why the old check saw only "a fence with no
# language" on this page.
LOST_OPENER = (
    "# Secrets\n\n## Rotation\n\n### Rotate the password\n\n"
    "# Step 1: Generate a new password\n"
    "NEW=$(openssl rand -hex 16)\n\n"
    "# Step 2: Set it\n"
    'aws rds modify-db-cluster --master-user-password "$NEW"\n'
    "```\n"
)

STRAY = {
    "lost opener": (LOST_OPENER, [7, 10]),
    "restored": (LOST_OPENER.replace("# Step 1", "```bash\n# Step 1"), []),
    "title only": ("# P\n\ntext\n", []),
    "second title": ("# P\n\n## A\n\n# Q\n", [5]),
    "title not first heading": ("## A\n\n# P\n", [3]),
    "indented is not a heading": ("# P\n\n   # Q\n", []),
    "indented four is code": ("# P\n\n    # Q\n", []),
    "no space is still a heading": ("# P\n\n#hashtag\n", [3]),
    "a wrapped sentence": ("# P\n\nthe routes in issues\n#217 and #219.\n", [4]),
    "not at the start": ("# P\n\nissues #217\nand #219.\n", []),
    "level two is fine": ("# P\n\n## Q\n\n## R\n", []),
    "in a fence": ("# P\n\n```bash\n# Step 1\n```\n", []),
    "in a tilde fence": ("# P\n\n~~~bash\n# Step 1\n~~~\n", []),
    "in an html comment": ("# P\n\n<!--\n# Q\n-->\n", []),
}


@pytest.mark.regression
@pytest.mark.parametrize("name", STRAY)
def test_a_title_after_the_first_heading_is_found_where_the_site_renders_one(name):
    """What the check calls a stray title is an <h1> after the page's first
    heading in what MkDocs renders."""
    text, lines = STRAY[name]
    assert dx.stray_titles(text) == lines
    html = _render(text)
    first = re.search(r"<h[1-6][ >]", html)
    h1s = len(re.findall(r"<h1[ >]", html))
    leading = 1 if first and first.group().startswith("<h1") else 0
    assert h1s - leading == len(lines)


def test_no_page_renders_a_title_the_check_cannot_see():
    """Pinned to the renderer on every walked page, so the rule cannot drift."""
    drift = {}
    for rel in sorted(dx.walk(REPO)):
        text = (REPO / rel).read_text(encoding="utf-8")
        html = _render(text)
        first = re.search(r"<h[1-6][ >]", html)
        leading = 1 if first and first.group().startswith("<h1") else 0
        rendered = len(re.findall(r"<h1[ >]", html)) - leading
        if rendered != len(dx.stray_titles(text)):
            drift[rel] = (rendered, dx.stray_titles(text))
    assert drift == {}


def test_front_matter_is_not_the_page():
    """MkDocs strips the YAML front matter before rendering; so does the rule."""
    assert dx.stray_titles("---\ntitle: x\n# a yaml comment\n---\n# P\n") == []


@pytest.mark.regression
def test_a_shell_block_that_lost_its_opening_fence_is_refused(repo):
    write(repo, "docs/lost.md", LOST_OPENER)
    body = GOOD.replace(
        'universe = ["docs/p.md"]', 'universe = ["docs/lost.md", "docs/p.md"]'
    )
    stray = [p for p in problems_of(enrol(repo, body)) if "level-1 heading" in p]
    assert [p.split(":", 2)[1] for p in stray] == ["7", "10"]
    assert all(p.startswith("docs/lost.md:") for p in stray)
    assert "lost its opening fence" in stray[0]


@pytest.mark.regression
def test_a_page_named_in_upper_case_is_in_the_universe(repo, tmp_path):
    write(tmp_path / "w", "docs/SNEAKY.MD", "# x\n")
    write(tmp_path / "w", "docs/Mixed.Md", "# x\n")
    assert dx.walk(tmp_path / "w") == {"docs/SNEAKY.MD", "docs/Mixed.Md"}
    write(repo, "docs/SNEAKY.MD", "# Sneaky\n\n```bash\ncurl -s x  # hidden\n```\n")
    found = problems_of(enrol(repo, GOOD))
    assert any(p.startswith("docs/SNEAKY.MD: a page") for p in found), found
    assert any(p.startswith("docs/SNEAKY.MD: 1 shell block") for p in found), found


# ---------------------------------------------------------------------------
# Fix round 2 (diff-of-diffs review): executed heredocs, URLs, CLAUDE.md case
# ---------------------------------------------------------------------------


# Round 3: no heredoc is exempt from the install/aws scans.  Every classifier
# of "data" heredocs was bypassable (`. <(cat <<EOF`, `(cat <<EOF) | bash`),
# so the rule is the dumb one, and these pin it -- including the cost.
HEREDOCS_THAT_RUN_OR_MIGHT = {
    "bash": ("bash <<'EOF'", "EOF"),
    "sh": ("sh <<EOF", "EOF"),
    "python3": ("python3 <<'PY'", "PY"),
    "node": ("node <<'JS'", "JS"),
    "ssh": ("ssh host <<EOF", "EOF"),
    "cat | bash": ("cat <<'EOF' | bash", "EOF"),
    "source <(cat": ("source <(cat <<'EOF'", "EOF\n)"),
    ". <(cat": (". <(cat <<EOF", "EOF\n)"),
    "bash <(cat": ("bash <(cat <<EOF", "EOF\n)"),
    "bash < <(cat": ("bash < <(cat <<EOF", "EOF\n)"),
    "(cat) | bash": ("(cat <<EOF", "EOF\n) | bash"),
    "cat > f (the accepted cost)": ("cat <<'EOF' > setup.txt", "EOF"),
    "tee f (the accepted cost)": ("tee setup.txt <<'EOF'", "EOF"),
}


@pytest.mark.regression
@pytest.mark.parametrize("name", HEREDOCS_THAT_RUN_OR_MIGHT)
@pytest.mark.parametrize(
    "line, message",
    [
        ("aws s3 rm --recursive s3://x", "may not call 'aws'"),
        ("npm install left-pad", "may not run 'npm'"),
    ],
    ids=["aws", "npm"],
)
def test_a_here_document_body_is_scanned_like_any_other_line(name, line, message):
    """Fail-closed: whatever receives the heredoc, its body is checked."""
    opener, close = HEREDOCS_THAT_RUN_OR_MIGHT[name]
    text = page(f"{FENCE}{{.bash exec}}\n{opener}\n{line}\n{close}\n{FENCE}")
    assert message in refused(text)


def test_a_registry_skip_counts_an_install_inside_a_here_document():
    """The registry predicate reads the same text the refusal does."""
    body = "sh <<'EOF'\npip install experimently\nEOF"
    (block,) = blocks(
        page(f'{FENCE}{{.bash skip reason="registry: not published"}}\n{body}\n{FENCE}')
    )
    assert dx.block_problems(block, "p.md") == []


def test_a_here_document_is_still_data_for_the_comment_rule():
    """zsh passes a heredoc body through untouched, so `#` there is not a comment."""
    body = "cat <<'EOF' > config.yml\n# PORT is the listen port\nEOF"
    assert dx.comment_lines(body) == []


@pytest.mark.regression
@pytest.mark.parametrize(
    "body",
    [
        "curl -o out.json \\\n  https://example.com/download/aws",
        "curl -sO \\\n  https://example.com/pip",
        "echo https://example.com/pip",
        "wget https://example.com/npm/aws",
        # only the ':' exclusion keeps these two from matching at a line start
        "https://example.com/pip",
        "(https://example.com/download/aws)",
        # only joining the continuation keeps these from matching at a line start
        "curl -o out.json \\\n  /tmp/downloads/aws",
        "tar -xzf bundle.tgz -C \\\n  ./vendor/pip",
    ],
)
def test_a_url_is_never_a_command(body):
    """An argument at the start of a continuation line, or a URL, is not a command."""
    (block,) = blocks(page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}"))
    assert dx.block_problems(block, "p.md") == []


def test_a_continued_line_still_finds_the_command():
    text = page(
        f"{FENCE}{{.bash exec}}\nFOO=1 \\\n  aws sts get-caller-identity\n{FENCE}"
    )
    assert "may not call 'aws'" in refused(text)


def test_working_instructions_are_excluded_in_any_case(tmp_path):
    for rel in ("CLAUDE.md", "docs/claude.md", "a/Claude.MD", "docs/real.md"):
        write(tmp_path, rel, "# x\n")
    assert dx.walk(tmp_path) == {"docs/real.md"}
    assert "working instructions" in dx._path_problem("t", "docs/claude.md", "enrolled")


# ---------------------------------------------------------------------------
# E0a-3: render every docs/ page, the walk against git, the zsh-lexer oracle
# ---------------------------------------------------------------------------


def _site(root: pathlib.Path, site: pathlib.Path, *rels: str) -> None:
    """A built site with a page for each of *rels*, through the site's extensions."""
    for rel in rels:
        stem = pathlib.PurePosixPath(rel).relative_to("docs").with_suffix("")
        out = site / stem / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_render((root / rel).read_text()))


@pytest.mark.regression
def test_render_covers_a_docs_page_that_is_not_enrolled(repo, tmp_path):
    """PE C4: a fence hidden in an HTML comment on a page with no shell block."""
    write(
        repo,
        "docs/other.md",
        f"# Other\n\n<!--\n{FENCE}http\nGET /health\n{FENCE}\n-->\n\nText.\n",
    )
    write(repo, "README.md", f"# Readme\n\n<!--\n{FENCE}text\nx\n{FENCE}\n-->\n")
    site = tmp_path / "site"
    _site(repo, site, "docs/p.md", "docs/other.md")
    with pytest.raises(dx.Refused) as excinfo:
        dx.render(site)
    # Outside docs/ nothing is rendered (T1's asymmetry), so README.md is silent.
    assert excinfo.value.problems == [
        "docs/other.md: 0 rendered code blocks, 1 fences in source"
    ]


def test_render_refuses_a_docs_page_the_site_does_not_have(repo, tmp_path):
    write(repo, "docs/sub/new.md", "# New\n")
    site = tmp_path / "site"
    _site(repo, site, "docs/p.md")
    with pytest.raises(dx.Refused) as excinfo:
        dx.render(site)
    assert excinfo.value.problems == [
        f"docs/sub/new.md: no page at {site / 'sub/new/index.html'} "
        "(excluded from the site?)"
    ]


def test_render_passes_when_every_page_rendered(repo, tmp_path):
    write(repo, "docs/README.md", f"# Home\n\n{FENCE}text\nx\n{FENCE}\n")
    site = tmp_path / "site"
    _site(repo, site, "docs/p.md")
    (site / "index.html").write_text(_render((repo / "docs/README.md").read_text()))
    dx.render(site)


def test_the_git_side_drops_only_working_instructions():
    listing = "\0".join(
        [
            "CLAUDE.md",
            "frontend/Claude.MD",
            ".claude/agents/x.md",
            "docs/A.MD",
            "docs/site/hidden.md",
            ".github/pull_request_template.md",
            "node_modules/x/README.md",
            "notes.txt",
            "",
        ]
    )
    assert dx.tracked_pages(listing) == {
        "docs/A.MD",
        "docs/site/hidden.md",
        ".github/pull_request_template.md",
        "node_modules/x/README.md",
    }


def _git_lists(monkeypatch, stdout: str, returncode: int = 0) -> None:
    import subprocess

    def fake(argv, **kwargs):
        assert argv[:2] == ["git", "-C"] and argv[3:] == ["ls-files", "-z"], argv
        return subprocess.CompletedProcess(argv, returncode, stdout, "fatal: no")

    monkeypatch.setattr(dx.subprocess, "run", fake)


@pytest.mark.regression
def test_a_tracked_page_the_walk_prunes_is_a_difference(repo, monkeypatch):
    """PE C2: `docs/site/new.md` is tracked, and the walk prunes `site`."""
    write(repo, "docs/site/new.md", "# Hidden\n")
    _git_lists(monkeypatch, "docs/p.md\0docs/site/new.md\0CLAUDE.md\0")
    with pytest.raises(dx.Refused) as excinfo:
        dx.compare_walk()
    assert excinfo.value.problems == [
        "docs/site/new.md: git tracks this page but the walk does not find it, so "
        "no rule is applied to it (a pruned or hidden directory?)"
    ]


def test_an_untracked_page_the_walk_finds_is_a_difference(repo, monkeypatch):
    write(repo, "docs/scratch.md", "# Scratch\n")
    _git_lists(monkeypatch, "docs/p.md\0")
    with pytest.raises(dx.Refused) as excinfo:
        dx.compare_walk()
    assert excinfo.value.problems == [
        "docs/scratch.md: the walk finds this page but git does not track it"
    ]


def test_the_walk_comparison_fails_closed_without_git(repo, monkeypatch):
    _git_lists(monkeypatch, "", returncode=128)
    with pytest.raises(dx.Refused, match="git ls-files failed"):
        dx.compare_walk()
    _git_lists(monkeypatch, "")
    with pytest.raises(dx.Refused, match="lists no Markdown page"):
        dx.compare_walk()


def test_the_walk_comparison_holds_when_they_agree(repo, monkeypatch):
    _git_lists(monkeypatch, "docs/p.md\0.claude/x.md\0")
    dx.compare_walk()


def _need_zsh(lines: int) -> None:
    """Skip, loudly and counted, where zsh is absent (Unit Tests, core-build).

    The render job installs zsh and fails on any skip in this file, so the skip
    can only happen where the oracle is not the thing being checked.
    """
    import shutil

    if shutil.which("zsh") is None:
        pytest.skip(
            f"zsh is not installed: the zsh-lexer oracle checked 0 of {lines} "
            "shell lines here (the Doc Examples render job installs zsh and runs it)"
        )


ZSH_SHAPES = [
    "echo docs#/x a#b $# ${#a[@]} *#*",
    "curl -s 'x#y' \"#z\"",
    "SLACK_DEFAULT_CHANNEL=#platform-alerts",
    "echo x   # trailing",
    "# whole line",
    "true;# after a separator",
    "(# in a subshell",
    "cat <<'EOF' > f.yml",
    "# data, not a comment",
    "EOF",
    "",
]


def test_the_zsh_oracle_agrees_on_the_comment_shapes(repo):
    write(
        repo,
        "docs/z.md",
        f"# Z\n\n{FENCE}bash\n" + "\n".join(ZSH_SHAPES) + f"\n{FENCE}\n",
    )
    lines = dx.shell_lines()
    _need_zsh(len(lines))
    # The heredoc's body and terminator are data, not shell; its opener is read.
    assert [t for rel, _, t, _ in lines if rel == "docs/z.md"] == [
        s for s in ZSH_SHAPES if s not in ("# data, not a comment", "EOF")
    ]
    dx.zsh_oracle()


@pytest.mark.regression
def test_the_zsh_oracle_fires_on_a_scanner_that_takes_any_hash(repo, monkeypatch):
    """The QA plant: comment_lines as "any unquoted #" disagrees with zsh."""
    write(repo, "docs/z.md", f"# Z\n\n{FENCE}bash\nX=#a\n{FENCE}\n")
    lines = dx.shell_lines()
    _need_zsh(len(lines))

    def any_hash(body):
        return [n for n, line in enumerate(body.split("\n")) if "#" in line]

    monkeypatch.setattr(dx, "comment_lines", any_hash)
    with pytest.raises(dx.Refused) as excinfo:
        dx.zsh_oracle()
    assert excinfo.value.problems == [
        "docs/z.md:4: zsh's lexer finds no comment and comment_lines does: 'X=#a'"
    ]


def test_the_zsh_oracle_is_refused_not_skipped_without_zsh(repo, monkeypatch):
    monkeypatch.setattr(dx.shutil, "which", lambda name, path=None: None)
    with pytest.raises(dx.Refused, match="zsh is not on PATH"):
        dx.zsh_oracle()


def test_comment_lines_agree_with_zsh_on_every_shell_line_in_the_repository():
    """The oracle over the real tree: every line outside a heredoc, every page."""
    _need_zsh(len(dx.shell_lines()))
    dx.zsh_oracle()


def test_the_render_job_runs_the_walk_zsh_and_zero_skip_checks():
    """The render job installs zsh, runs both CI-only checks and allows no skip."""
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "doc-examples.yml").read_text()
    )
    runs = [s.get("run", "") for s in workflow["jobs"]["render"]["steps"]]
    install = next(i for i, r in enumerate(runs) if "apt-get install -y -qq zsh" in r)
    assert any("zsh --version" in r for r in runs)
    for command in (
        "python scripts/doc_examples.py --walk-vs-git",
        "python scripts/doc_examples.py --zsh-oracle",
        "python scripts/doc_examples.py --render site",
    ):
        assert command in runs, command
    assert install < runs.index("python scripts/doc_examples.py --zsh-oracle")
    tests = next(r for r in runs if "test_doc_examples.py" in r)
    assert "--junitxml=docs-junit.xml || rc=1" in tests
    # The skip check runs after a failing test too (a failure hid it once).
    assert (
        "scripts/check_junit_skips.py docs-junit.xml backend/tests/unit/docs "
        "backend/tests/unit/scripts || rc=1" in tests
    )
    assert tests.rstrip().endswith('exit "$rc"')
    # A failed version step (no zsh) must not skip the contract or the others.
    steps = workflow["jobs"]["render"]["steps"]
    after = [s for s in steps[install + 2 :] if "run" in s]
    assert after and all(s.get("if") == "${{ !cancelled() }}" for s in after), [
        s["name"] for s in after if s.get("if") != "${{ !cancelled() }}"
    ]


# ---------------------------------------------------------------------------
# The demotion guard (scripts/check_doc_demotions.py), run by the required
# `regression-guard` job: an example that stops running needs a label.
# These tests use git, but only in repositories they create under tmp_path.
# ---------------------------------------------------------------------------

_gspec = importlib.util.spec_from_file_location(
    "check_doc_demotions", REPO / "scripts" / "check_doc_demotions.py"
)
dd = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(dd)

GUARD_WORKFLOW = REPO / ".github" / "workflows" / "regression-guard.yml"
GUARD_STEP = "A documentation example that stops running must be labelled"


def enrolment(**docs: int) -> str:
    """A toml with one [[document]] per keyword: path stem -> exec count."""
    return "[meta]\n" + "".join(
        f'\n[[document]]\npath = "docs/{name}.md"\nexec = {count}\nskip = 0\n'
        for name, count in docs.items()
    )


def guard(tmp_path, base: str, head: str, labels: str = "[]", capsys=None):
    (tmp_path / "base.toml").write_text(base)
    (tmp_path / "head.toml").write_text(head)
    status = dd.main(
        [
            "--base-file",
            str(tmp_path / "base.toml"),
            "--head-file",
            str(tmp_path / "head.toml"),
            "--labels",
            labels,
        ]
    )
    return status, (capsys.readouterr().out if capsys else "")


def test_an_exec_drop_without_the_label_is_red(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    status, out = guard(
        tmp_path,
        enrolment(quick=11, create=9),
        enrolment(quick=10, create=9),
        capsys=capsys,
    )
    assert status == 1
    assert (
        "docs/quick.md: exec 11 -> 10 against the base; label the pull request "
        "docs-demotion if this is intended" in out
    )
    assert "create" not in out


def test_the_same_drop_with_the_label_is_green(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    status, out = guard(
        tmp_path,
        enrolment(quick=11),
        enrolment(quick=10),
        labels='["bug", "docs-demotion"]',
        capsys=capsys,
    )
    assert status == 0
    assert "warning: docs/quick.md: exec 11 -> 10" in out  # still reported
    assert "1 demotion(s) accepted" in out


def test_another_label_does_not_waive_a_demotion(tmp_path):
    status, _ = guard(
        tmp_path, enrolment(quick=11), enrolment(quick=10), labels='["docs-demotions"]'
    )
    assert status == 1


def test_the_comparison_is_per_document_not_by_total(tmp_path, capsys):
    """One page gaining an example does not pay for another losing one."""
    status, out = guard(
        tmp_path, enrolment(a=3, b=3), enrolment(a=2, b=4), capsys=capsys
    )
    assert status == 1
    assert "docs/a.md: exec 3 -> 2" in out


def test_a_document_that_leaves_the_enrolment_is_a_drop_to_zero(tmp_path, capsys):
    status, out = guard(tmp_path, enrolment(a=3, b=1), enrolment(b=1), capsys=capsys)
    assert status == 1
    assert "docs/a.md: exec 3 -> 0 against the base (no longer enrolled)" in out


def test_a_zero_exec_document_may_leave_and_counts_may_rise(tmp_path, capsys):
    status, out = guard(
        tmp_path, enrolment(a=0, b=1), enrolment(b=2, c=1), capsys=capsys
    )
    assert status == 0, out
    assert "No documentation example stopped running (2 documents on the base)" in out


@pytest.mark.parametrize(
    "base, head, labels, message",
    [
        ("[[document]\n", enrolment(a=1), "[]", "the base scripts/doc_examples.toml"),
        (enrolment(a=1), "not toml =", "[]", "the pull request's scripts/doc_examples"),
        ('[[document]]\npath = "docs/a.md"\n', enrolment(a=1), "[]", "whole-number"),
        (
            '[[document]]\npath = "docs/a.md"\nexec = true\n',
            enrolment(a=1),
            "[]",
            "whole",
        ),
        (enrolment(a=1) + enrolment(a=1)[7:], enrolment(a=1), "[]", "enrolled twice"),
        (enrolment(a=1), enrolment(a=1), "bug", "not a JSON array"),
        # The label acknowledges a demotion someone saw; not an unreadable base.
        ("[[document]\n", enrolment(a=1), '["docs-demotion"]', "not valid TOML"),
    ],
)
def test_an_unreadable_side_is_red_never_no_demotions(
    tmp_path, capsys, base, head, labels, message
):
    status, out = guard(tmp_path, base, head, labels, capsys=capsys)
    assert status == 2
    assert message in out
    assert "Refusing to report no demotions" in out
    assert "No documentation example" not in out


def test_a_missing_base_file_is_red(tmp_path, capsys):
    (tmp_path / "head.toml").write_text(enrolment(a=1))
    status = dd.main(
        [
            "--base-file",
            str(tmp_path / "absent.toml"),
            "--head-file",
            str(tmp_path / "head.toml"),
        ]
    )
    assert status == 2
    assert "cannot read the base toml" in capsys.readouterr().out


def test_an_empty_base_ref_is_red(capsys):
    """`github.event.pull_request.base.sha` empty is an unknown base, not HEAD."""
    assert dd.main(["--base-ref", ""]) == 2
    assert "the base commit is unknown" in capsys.readouterr().out


def _git_env(tmp_path) -> dict:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        HOME=str(tmp_path),
    )
    env.pop("GITHUB_ACTIONS", None)
    # The step calls `python3`; make that this interpreter (tomllib needs 3.11).
    shims = tmp_path / "bin"
    shims.mkdir(exist_ok=True)
    (shims / "python3").symlink_to(sys.executable)
    env["PATH"] = f"{shims}{os.pathsep}{env.get('PATH', '')}"
    return env


def _git(cwd, env, *args) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def pr_checkout(tmp_path):
    """An `origin` whose main has quick=11, and a full clone (as fetch-depth: 0
    leaves it) at a pull request that lowers it to 10: (clone, env, base_sha)."""
    env = _git_env(tmp_path)
    origin = tmp_path / "origin"
    (origin / "scripts").mkdir(parents=True)
    _git(origin, env, "init", "-q", "-b", "main")
    (origin / "scripts" / "doc_examples.toml").write_text(enrolment(quick=11))
    (origin / "scripts" / "check_doc_demotions.py").write_bytes(
        (REPO / "scripts" / "check_doc_demotions.py").read_bytes()
    )
    _git(origin, env, "add", ".")
    _git(origin, env, "commit", "-q", "-m", "base")
    base_sha = _git(origin, env, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    _git(tmp_path, env, "clone", "-q", str(origin), str(clone))
    (clone / "scripts" / "doc_examples.toml").write_text(enrolment(quick=10))
    _git(clone, env, "commit", "-q", "-am", "demote")
    return clone, env, base_sha


def _guard_job() -> dict:
    return yaml.safe_load(GUARD_WORKFLOW.read_text())["jobs"]["regression-guard"]


def _step_script() -> str:
    (step,) = [s for s in _guard_job()["steps"] if s.get("name") == GUARD_STEP]
    return step["run"]


def _run_step(clone, env, base_sha, labels="[]", script=None):
    import subprocess

    return subprocess.run(
        ["bash", "-c", script if script is not None else _step_script()],
        cwd=clone,
        env={**env, "BASE_SHA": base_sha, "LABELS": labels},
        capture_output=True,
        text=True,
    )


def test_the_step_itself_is_red_on_a_drop_and_green_with_the_label(pr_checkout):
    clone, env, base_sha = pr_checkout
    red = _run_step(clone, env, base_sha)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "docs/quick.md: exec 11 -> 10" in red.stdout
    green = _run_step(clone, env, base_sha, labels='["docs-demotion"]')
    assert green.returncode == 0, green.stdout + green.stderr


def test_the_step_is_red_when_the_base_is_missing(pr_checkout):
    """An unknown SHA is `not our ref`; an EMPTY one would be `+:ref`, which git
    reads as the remote's HEAD and fetches happily -- the step refuses it."""
    clone, env, _ = pr_checkout
    for missing in ("0" * 40, ""):
        result = _run_step(clone, env, missing, labels='["docs-demotion"]')
        assert result.returncode != 0, (missing, result.stdout)
        assert "No documentation example" not in result.stdout


def test_removing_the_fetch_makes_the_step_red_not_green(pr_checkout):
    """The clone already holds the base commit (fetch-depth: 0), so reading
    "$BASE_SHA" directly would pass with no fetch at all. The step reads only
    the ref its fetch writes, so deleting the fetch is `invalid object name`."""
    clone, env, base_sha = pr_checkout
    script = _step_script()
    without_fetch = "\n".join(
        line
        for line in script.splitlines()
        if not line.lstrip().startswith("git fetch")
    )
    assert without_fetch != script
    result = _run_step(
        clone, env, base_sha, labels='["docs-demotion"]', script=without_fetch
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot read scripts/doc_examples.toml at the base" in result.stdout
    assert "'refs/doc-demotion/base'. Refusing to report no demotions." in result.stdout


def test_the_guard_is_its_own_step_in_the_required_job():
    """Branch protection requires the JOB `regression-guard`. The bug check's
    script ends in `exit 0` on an unlabelled pull request, so the guard is a
    separate step, unconditional but for cancellation, with no fail-open."""
    doc = yaml.safe_load(GUARD_WORKFLOW.read_text())
    triggers = doc[True]["pull_request"]  # PyYAML reads the key `on` as True
    assert {"labeled", "unlabeled", "synchronize", "opened"} <= set(triggers["types"])
    assert "paths" not in triggers and "paths-ignore" not in triggers
    job = doc["jobs"]["regression-guard"]
    assert job["name"] == "regression-guard"
    assert "if" not in job
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    (step,) = [s for s in job["steps"] if s.get("name") == GUARD_STEP]
    assert step.get("if") == "${{ !cancelled() }}"
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert (
        step["env"]["LABELS"]
        == "${{ toJSON(github.event.pull_request.labels.*.name) }}"
    )
    run = step["run"]
    assert run.startswith("set -euo pipefail\n")
    assert ': "${BASE_SHA:?' in run
    assert "|| true" not in run and "/dev/null" not in run and "exit 0" not in run
    assert 'git fetch --no-tags origin "+${BASE_SHA}:refs/doc-demotion/base"' in run
    assert "--base-ref refs/doc-demotion/base" in run
    others = [s.get("run", "") for s in job["steps"] if s is not step]
    assert not any("check_doc_demotions" in r for r in others)


# ---------------------------------------------------------------------------
# E0b: shards, the summary, the run deadline and the measured count
# ---------------------------------------------------------------------------

DOC_EXAMPLES_WORKFLOW = REPO / ".github" / "workflows" / "doc-examples.yml"


def _doc(path: str, environment: str = "stack", exec_count: int = 3) -> dict:
    return {
        "path": path,
        "environment": environment,
        "exec": exec_count,
        "skip": 0,
        "expects": 0,
    }


# Thirty pages: both profiles, bare pages that run and pages that run nothing.
MANY = (
    [_doc(f"docs/s{i:02}.md") for i in range(17)]
    + [_doc(f"docs/b{i:02}.md", "bare", 2) for i in range(4)]
    + [_doc(f"docs/f{i:02}.md", "stack-full", 1) for i in range(7)]
    + [_doc("docs/none.md", "bare", 0), _doc("docs/zero-stack.md", "stack", 0)]
)


def _assert_partition(documents: list[dict]) -> dict:
    plan = dx.shard_plan(documents)
    runs = {d["path"] for d in documents if d["exec"] > 0}
    union = [p for pages in plan.values() for p in pages]
    assert sorted(union) == sorted(runs), (
        "the union of the shards is every page that runs"
    )
    assert len(union) == len(set(union)), "the shards are disjoint"
    environment = {d["path"]: d["environment"] for d in documents}
    for (profile, k, n), pages in plan.items():
        assert 1 <= k <= n and pages
        assert len(pages) <= dx.PAGES_PER_SHARD, (profile, k, n, pages)
        assert {environment[p] == "stack-full" for p in pages} == {profile == "full"}
    return plan


def test_the_shards_are_every_page_that_runs_once():
    plan = _assert_partition(MANY)
    assert [dx.shard_label(s) for s in plan] == [
        "core:1/4",
        "core:2/4",
        "core:3/4",
        "core:4/4",
        "full:1/2",
        "full:2/2",
    ]
    assert dx.shard_plan(list(reversed(MANY))) == plan, "order-independent"
    assert dx.matrix(plan)["include"][4] == {"profile": "full", "shard": 1, "of": 2}


def test_the_repository_s_shards_are_every_page_that_runs_once():
    plan = _assert_partition(dx.load_enrolment())
    assert plan, "no page runs: the matrix would be empty"


def test_a_shard_fits_its_budget_by_construction():
    """The per-shard budget is a function of measured numbers, not a timing
    assertion: PAGES_PER_SHARD pages of PAGE_SECONDS each fit the budget, and
    the budget leaves the job's setup inside the 10-minute shard."""
    assert dx.PAGES_PER_SHARD * dx.PAGE_SECONDS <= dx.SHARD_BUDGET_SECONDS
    assert dx.SHARD_BUDGET_SECONDS + 90 <= 600
    assert dx.PAGES_PER_SHARD >= 1


@pytest.mark.parametrize(
    "text", ["core", "core:0/2", "core:3/2", "full:1", "cloud:1/1", "core:1/2 "]
)
def test_a_malformed_shard_is_refused(text):
    with pytest.raises(dx.Refused):
        dx.parse_shard(text)


def _report(directory: pathlib.Path, shard, pages: dict, stopped=None) -> None:
    profile, k, _ = shard
    dx.write_report(directory / f"{profile}-{k}" / "r.json", shard, pages, stopped)


def _ok(doc: dict, reached=None, passed=True) -> dict:
    return {
        "exec": doc["exec"],
        "reached": doc["exec"] if reached is None else reached,
        "passed": passed,
        "seconds": 61.5,
    }


@pytest.fixture
def many(tmp_path, monkeypatch):
    """The MANY enrolment as the summary reads it, and every shard's clean report."""
    enrolment = dx.Enrolment(
        MANY, [], {}, [], "doc_examples.toml", [], sum(d["exec"] for d in MANY)
    )
    monkeypatch.setattr(dx, "load", lambda path=None: enrolment)
    reports = tmp_path / "reports"
    by_path = {d["path"]: d for d in MANY}
    for shard, pages in dx.shard_plan(MANY).items():
        _report(reports, shard, {p: _ok(by_path[p]) for p in pages})
    return reports


def _summary_problems(directory) -> str:
    with pytest.raises(dx.Failed) as excinfo:
        dx.summarise(directory)
    return str(excinfo.value)


def test_the_summary_passes_when_every_page_ran_once(many, capsys):
    dx.summarise(many)
    out = capsys.readouterr().out
    total = sum(d["exec"] for d in MANY)
    assert f"measured: 28/28 pages ran, {total} exec blocks reached" in out
    assert "6 shard(s): every page ran once and every block ran" in out


def test_no_report_at_all_is_a_failure(tmp_path, many):
    assert "no shard reports" in _summary_problems(tmp_path / "nothing-downloaded")
    (tmp_path / "empty").mkdir()
    assert "no shard reports" in _summary_problems(tmp_path / "empty")


def test_a_shard_that_never_ran_is_named_with_its_pages(many):
    missing = dx.shard_plan(MANY)[("core", 2, 4)]
    for report in (many / "core-2").iterdir():
        report.unlink()
    problems = _summary_problems(many)
    assert "shard core:2/4 sent no report" in problems
    assert all(page in problems for page in missing)


def test_a_page_dropped_from_every_shard_is_named(many, monkeypatch):
    """The plan loses a page (the tamper): the summary's expected pages come from
    the enrolment, not from the plan, so the loss is still seen."""
    real = dx.shard_plan

    def lossy(documents):
        return {
            s: [p for p in pages if p != "docs/s05.md"]
            for s, pages in real(documents).items()
        }

    monkeypatch.setattr(dx, "shard_plan", lossy)
    for directory in many.iterdir():
        for report in directory.iterdir():
            data = json.loads(report.read_text())
            data["pages"].pop("docs/s05.md", None)
            report.write_text(json.dumps(data))
    problems = _summary_problems(many)
    assert "docs/s05.md: in no shard's report; it did not run" in problems
    assert "exec blocks reached their end across the shards; [meta] exec" in problems


def test_a_page_that_ran_twice_is_refused(many):
    extra = many / "core-1" / "r.json"
    data = json.loads(extra.read_text())
    other = dx.shard_plan(MANY)[("core", 2, 4)][0]
    data["pages"][other] = _ok(_doc(other))
    extra.write_text(json.dumps(data))
    assert f"{other}: ran 2 times (core:1/4, core:2/4)" in _summary_problems(many)


def test_a_dropped_block_in_the_measured_count_is_refused(many):
    """A shard that ran a page's blocks but one: reached < exec, and the total
    no longer equals [meta] exec."""
    report = many / "full-1" / "r.json"
    data = json.loads(report.read_text())
    page = sorted(data["pages"])[0]
    data["pages"][page]["reached"] -= 1
    report.write_text(json.dumps(data))
    problems = _summary_problems(many)
    assert f"{page}: 0 exec blocks reached their end; the enrolment says 1" in problems
    total = sum(d["exec"] for d in MANY)
    assert f"{total - 1} exec blocks reached their end across the shards" in problems


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda d: d["pages"][sorted(d["pages"])[0]].update(passed=False),
            "FAILED in shard",
        ),
        (lambda d: d.update(shard="core:9/9"), "is not in the plan"),
        (lambda d: d.update(version=0), "not a version-1 shard report"),
        (
            lambda d: d.update(stopped="the run deadline was reached"),
            "the run deadline",
        ),
        (
            lambda d: d["pages"][sorted(d["pages"])[0]].update(reached="all"),
            "malformed result",
        ),
    ],
    ids=["failed-page", "unplanned-shard", "version", "stopped", "malformed"],
)
def test_the_summary_refuses_a_bad_report(many, mutate, message):
    report = many / "core-1" / "r.json"
    data = json.loads(report.read_text())
    mutate(data)
    report.write_text(json.dumps(data))
    assert message in _summary_problems(many)


def test_the_same_shard_twice_is_refused(many):
    (many / "again").mkdir()
    (many / "again" / "r.json").write_text((many / "core-1" / "r.json").read_text())
    assert "shard core:1/4 reported twice" in _summary_problems(many)


# --- the runner's side of a shard, with no Docker -------------------------


def _shard_repo(repo, pages: int, exec_count: int = 2) -> pathlib.Path:
    """A scratch repository whose pages run (``bare``), each with exec_count blocks."""
    block = f"{FENCE}{{.bash exec}}\necho hi\n{FENCE}\n<!-- expect: hi -->"
    paths = [f"docs/q{i:02}.md" for i in range(pages)]
    for rel in paths:
        write(repo, rel, page(*[block] * exec_count))
    (repo / "docs" / "p.md").unlink()
    body = (
        f"[meta]\ndocuments = {pages}\nexec = {pages * exec_count}\n"
        f"universe = {json.dumps(paths)}\n"
    )
    for rel in paths:
        body += (
            f'\n[[document]]\npath = "{rel}"\nenvironment = "bare"\n'
            f"exec = {exec_count}\nskip = 0\nexpects = {exec_count}\n"
        )
    return enrol(repo, body)


class FakePages:
    """Stands in for run_document: records what ran, and reports its blocks."""

    def __init__(self, drop=None, after=None):
        self.ran: list[str] = []
        self.drop = drop  # a page whose last block silently does not run
        self.after = after  # called after each page (a clock to advance)

    def __call__(self, doc, blocks, index, overrides, stack_up, out, deadline, full_up):
        self.ran.append(doc["path"])
        reached = sum(b.kind == "exec" for b in blocks)
        if doc["path"] == self.drop:
            reached -= 1
        if self.after:
            self.after()
        return [], reached


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr(dx, "_volumes", lambda: set())
    monkeypatch.setattr(dx, "_report_dropped", lambda out: None)


def test_a_shard_runs_its_pages_only_and_reports_what_it_measured(
    repo, no_docker, tmp_path, capsys
):
    toml = _shard_repo(repo, 8)
    plan = dx.shard_plan(dx.load_enrolment(toml))
    assert list(plan) == [("core", 1, 2), ("core", 2, 2)]
    fake = FakePages()
    report = tmp_path / "out" / "core-2.json"
    dx.run(toml, shard=("core", 2, 2), report=report, run_page=fake)
    assert fake.ran == plan[("core", 2, 2)]
    out = capsys.readouterr().out
    assert "docs/q01.md: passed (2/2 exec blocks ran) in " in out
    assert (
        "shard core:2/2: 4 page(s), 8 exec blocks reached their end (measured)" in out
    )
    data = json.loads(report.read_text())
    assert data["shard"] == "core:2/2" and data["stopped"] is None
    assert set(data["pages"]) == set(plan[("core", 2, 2)])
    assert all(r["reached"] == 2 and r["passed"] for r in data["pages"].values())


def test_a_dropped_block_fails_the_page_and_reaches_the_report(
    repo, no_docker, tmp_path, capsys
):
    """The count printed and reported is measured (the plant: one block of a
    page never runs), not copied from the enrolment."""
    toml = _shard_repo(repo, 3)
    report = tmp_path / "r.json"
    with pytest.raises(dx.Failed) as excinfo:
        dx.run(
            toml,
            shard=("core", 1, 1),
            report=report,
            run_page=FakePages(drop="docs/q01.md"),
        )
    assert "docs/q01.md: 1 exec blocks reached their end; the enrolment says 2" in str(
        excinfo.value
    )
    assert "docs/q01.md: FAILED (1/2 exec blocks ran)" in capsys.readouterr().out
    page_result = json.loads(report.read_text())["pages"]["docs/q01.md"]
    assert (page_result["reached"], page_result["passed"]) == (1, False)


def test_a_shard_the_plan_does_not_have_is_refused(repo, no_docker):
    toml = _shard_repo(repo, 3)
    with pytest.raises(dx.Refused, match="shard core:1/2 is not in the plan"):
        dx.run(toml, shard=("core", 1, 2), run_page=FakePages())


def test_the_deadline_stops_the_shard_and_names_what_did_not_run(
    repo, no_docker, tmp_path, capsys
):
    """A fake clock: the deadline passes while the second page runs, so the third
    never starts, and the report says so."""
    toml = _shard_repo(repo, 3)
    now = [1000.0]
    deadline = dx.Deadline(at=1150.0, clock=lambda: now[0])

    def tick():
        now[0] += 100

    report = tmp_path / "r.json"
    with pytest.raises(dx.Failed) as excinfo:
        dx.run(
            toml,
            shard=("core", 1, 1),
            report=report,
            deadline=deadline,
            run_page=FakePages(after=tick),
        )
    assert "docs/q02.md: not run: the run deadline (" in str(excinfo.value)
    assert "docs/q02.md: NOT RUN (run deadline)" in capsys.readouterr().out
    data = json.loads(report.read_text())
    assert data["stopped"].startswith("the run deadline (")
    assert data["pages"]["docs/q02.md"] == {
        "exec": 2,
        "reached": 0,
        "passed": False,
        "seconds": 0,
    }


def test_the_deadline_kills_the_process_group_and_names_the_block(tmp_path):
    """A block that would outlive the job: the runner kills its group at the
    deadline, before the block's own timeout, and names the page and block."""
    pidfile = tmp_path / "pid"
    text = page(
        f"{FENCE}{{.bash exec}}\necho first\n{FENCE}\n<!-- expect: first -->",
        f"{FENCE}{{.bash exec timeout=600}}\nsleep 300 & echo $! > {pidfile}; wait\n{FENCE}",
    )
    deadline = dx.Deadline(at=dx.time.time() + 2)
    problems, reached = dx.execute_counted(
        blocks(text), "p.md", dx.scrubbed_env("docex-unit", {}), deadline
    )
    joined = "\n".join(problems)
    assert reached == 1
    assert "p.md:8: block did not reach its end (the run deadline (" in joined
    assert "was reached; process group killed)" in joined
    background = int(pidfile.read_text())
    assert _dies(background), f"the background sleep {background} is still running"


def test_a_stack_start_stops_at_the_deadline_too(tmp_path):
    deadline = dx.Deadline(at=dx.time.time() + 1)
    rc, _, _, why = dx.run_bounded(
        ["bash", "-c", "sleep 60"], dx.scrubbed_env("docex-unit", {}), 600, deadline
    )
    assert rc is None and why.startswith("the run deadline (")
    rc, _, _, why = dx.run_bounded(
        ["bash", "-c", "sleep 60"], dx.scrubbed_env("docex-unit", {}), 1, None
    )
    assert rc is None and why == "timed out after 1s; process group killed"
    assert dx.run_bounded(["true"], dx.scrubbed_env("docex-unit", {}), 5)[0] == 0


@pytest.mark.parametrize(
    "environ, expected",
    [
        ({}, None),
        (
            {"DOCEX_JOB_START": "1000", "DOCEX_JOB_TIMEOUT_MINUTES": "15"},
            1000 + 900 - 120,
        ),
        ({"DOCEX_JOB_START": "1000"}, "must both be whole numbers"),
        ({"DOCEX_JOB_TIMEOUT_MINUTES": "15"}, "must both be whole numbers"),
        (
            {"DOCEX_JOB_START": "soon", "DOCEX_JOB_TIMEOUT_MINUTES": "15"},
            "whole numbers",
        ),
        (
            {"DOCEX_JOB_START": "1000", "DOCEX_JOB_TIMEOUT_MINUTES": "2"},
            "leaves nothing",
        ),
        ({"DOCEX_JOB_START": "10", "DOCEX_JOB_TIMEOUT_MINUTES": "15"}, "passed before"),
    ],
    ids=["unset", "set", "no-timeout", "no-start", "not-a-number", "too-short", "past"],
)
def test_the_deadline_comes_from_the_job_s_start_and_timeout(environ, expected):
    clock = lambda: 1100.0  # noqa: E731
    if isinstance(expected, str):
        with pytest.raises(dx.Refused, match=expected):
            dx.deadline_from_env(environ, clock)
    elif expected is None:
        assert dx.deadline_from_env(environ, clock) is None
    else:
        assert dx.deadline_from_env(environ, clock).at == expected


def test_a_shard_in_github_actions_without_a_deadline_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("DOCEX_JOB_START", raising=False)
    monkeypatch.delenv("DOCEX_JOB_TIMEOUT_MINUTES", raising=False)
    assert dx.main(["--run", "--shard", "core:1/1"]) == 2
    assert "needs its deadline" in capsys.readouterr().err


# --- stack-full ------------------------------------------------------------


def test_the_full_profile_probe(tmp_path):
    full = {"profile": "full", "modules": ["rbac"], "version": "1"}
    assert dx.full_profile_problem("p.md", lambda url: full) is None
    core = dx.full_profile_problem("p.md", lambda url: {"profile": "core"})
    assert core == (
        "p.md: the full profile did not load: GET /api/v1/modules says profile 'core'"
    )

    def refused(url):
        raise OSError("connection refused")

    assert "failed (connection refused)" in dx.full_profile_problem("p.md", refused)


@pytest.mark.parametrize(
    "error",
    [http.client.IncompleteRead(b"{"), http.client.BadStatusLine("")],
    ids=["incomplete-read", "bad-status-line"],
)
def test_the_full_profile_probe_names_a_cut_off_answer(error):
    """#207: http.client's own errors are not OSErrors; the probe names them
    as the page's problem instead of letting them end the shard."""

    def cut_off(url):
        raise error

    problem = dx.full_profile_problem("p.md", cut_off)
    assert problem.startswith(
        f"p.md: the full profile did not load: GET {dx.MODULES_URL} failed ("
    )


def test_the_full_profile_probe_survives_a_dropped_connection(monkeypatch):
    """The real fetch, against a server that promises a body and hangs up
    halfway: urllib raises IncompleteRead, and the probe reports it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def answer_half():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b'Content-Length: 100\r\n\r\n{"profile"'
            )

    thread = threading.Thread(target=answer_half, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/api/v1/modules"
    monkeypatch.setattr(dx, "MODULES_URL", url)
    try:
        problem = dx.full_profile_problem("p.md")
    finally:
        thread.join(timeout=10)
        server.close()
    assert problem is not None
    assert problem.startswith(
        f"p.md: the full profile did not load: GET {url} failed ("
    )
    assert "IncompleteRead" in problem or "bytes read" in problem


@pytest.mark.parametrize(
    "remaining, expected",
    [
        (None, 600),
        (10_000.0, 600),
        (300.0, 360),
        (0.0, dx.TEARDOWN_RESERVE_SECONDS),
        (-30.0, dx.TEARDOWN_RESERVE_SECONDS),
    ],
    ids=["no-deadline", "capped", "before-the-deadline", "at-it", "past-it"],
)
def test_a_teardown_is_given_the_reserve_at_least(remaining, expected):
    deadline = (
        None if remaining is None else dx.Deadline(at=remaining, clock=lambda: 0.0)
    )
    assert dx._teardown_seconds(deadline) == expected


def test_the_teardown_reserve_fits_inside_the_margin():
    """A teardown that starts at the deadline ends inside the margin, with
    time left for the report and the job's later steps (measured at up to
    25 s, plus up to 4 s of job clock before DOCEX_JOB_START); and the
    reserve stays well above the slowest full-profile teardown measured
    (10.8 s; see TEARDOWN_RESERVE_SECONDS)."""
    assert dx.DEADLINE_MARGIN_SECONDS - dx.TEARDOWN_RESERVE_SECONDS >= 25 + 4
    assert dx.TEARDOWN_RESERVE_SECONDS >= 2 * 10.8


def test_a_deadline_during_the_full_stack_start_is_named_and_torn_down(monkeypatch):
    """#207: the deadline lands while the stack is starting (a start that never
    becomes healthy).  The start's process group is killed, the page's problem
    names the start and the deadline, the probe is never asked, and the
    project is still torn down with the reserve."""
    never_healthy = "sleep 300"
    monkeypatch.setattr(dx, "STACK_FULL_UP", never_healthy)
    monkeypatch.setattr(dx, "preflight", lambda env: None)
    monkeypatch.setattr(dx, "project_resources", lambda project: [])

    def probe(rel, fetch=None):
        raise AssertionError("the probe ran after a start that did not finish")

    monkeypatch.setattr(dx, "full_profile_problem", probe)
    teardowns = []

    def docker(*args, env=None, timeout=120):
        teardowns.append((args, env["COMPOSE_PROJECT_NAME"], timeout))
        return dx.subprocess.CompletedProcess(["docker", *args], 0, "", "")

    monkeypatch.setattr(dx, "_docker", docker)
    # A clock that stands still: the deadline is always 1 s away, so the start
    # is killed by the deadline and never by its own 1200 s timeout.
    deadline = dx.Deadline(at=1001.0, clock=lambda: 1000.0)
    doc = {"path": "p.md", "environment": "stack-full", "exec": 1}
    start = dx.Block(
        line=3, kind="exec", lang="bash", body=never_healthy + "\n", timeout=1200
    )
    out = io.StringIO()
    problems, reached = dx.run_document(
        doc, [], 0, {}, None, out, deadline, stack_full_up=start
    )
    assert reached == 0
    assert len(problems) == 1
    assert problems[0].startswith(
        f"p.md: the stack did not start ({never_healthy}): the run deadline "
        f"({deadline.describe()}) was reached; process group killed"
    )
    [(args, project, timeout)] = teardowns
    assert args == ("compose", "down", "-v", "--remove-orphans")
    assert project.startswith("docex-")
    assert timeout == dx._teardown_seconds(deadline) >= dx.TEARDOWN_RESERVE_SECONDS
    assert "p.md: teardown took " in out.getvalue()


def test_a_stack_full_page_needs_the_modules_page_s_one_full_start(repo):
    getting = repo / "docs" / "getting-started"
    getting.mkdir()
    one = f"{FENCE}{{.bash exec}}\n{dx.STACK_FULL_UP}\n{FENCE}"
    body = textwrap.dedent(
        """
        [meta]
        documents = 2
        exec = {total}
        universe = ["docs/getting-started/modules.md", "docs/p.md"]

        [[document]]
        path = "docs/p.md"
        environment = "stack-full"
        exec = 1
        skip = 1
        expects = 1

        [[document]]
        path = "docs/getting-started/modules.md"
        environment = "{env}"
        exec = {n}
        skip = 0
        expects = 0
        """
    )
    (getting / "modules.md").write_text(page(one))
    dx.check(enrol(repo, body.format(n=1, total=2, env="stack-full")))
    assert any(
        "needs docs/getting-started/modules.md enrolled as 'stack-full'" in p
        for p in problems_of(enrol(repo, body.format(n=1, total=2, env="bare")))
    )
    (getting / "modules.md").write_text(page(one, one))
    assert any(
        "2 exec blocks equal" in p
        for p in problems_of(enrol(repo, body.format(n=2, total=3, env="stack-full")))
    )


def test_a_full_page_is_refused_in_a_core_tree(repo, no_docker):
    """No modules/ directory: a stack-full page cannot run, and says to run a
    core shard, rather than failing every module call with a 404."""
    getting = repo / "docs" / "getting-started"
    getting.mkdir()
    (getting / "modules.md").write_text(
        page(f"{FENCE}{{.bash exec}}\n{dx.STACK_FULL_UP}\n{FENCE}")
    )
    toml = enrol(
        repo,
        """
        [meta]
        documents = 1
        exec = 1
        universe = ["docs/getting-started/modules.md", "docs/p.md"]

        [[document]]
        path = "docs/getting-started/modules.md"
        environment = "stack-full"
        exec = 1
        skip = 0
        expects = 0
        """,
    )
    (repo / "docs" / "p.md").write_text("# no shell\n")
    with pytest.raises(
        dx.Refused, match="needs the full profile, and this tree has no"
    ):
        dx.run(toml, shard=("full", 1, 1), run_page=FakePages())
    (repo / "modules").mkdir()
    fake = FakePages()
    dx.run(toml, shard=("full", 1, 1), run_page=fake)
    assert fake.ran == ["docs/getting-started/modules.md"]


# --- the workflow ----------------------------------------------------------


def _jobs() -> dict:
    return yaml.safe_load(DOC_EXAMPLES_WORKFLOW.read_text())["jobs"]


def test_the_matrix_is_the_runner_s_plan_and_one_red_shard_cancels_nothing():
    jobs = _jobs()
    plan_step = [s for s in jobs["plan"]["steps"] if s.get("id") == "plan"][0]
    assert "python scripts/doc_examples.py --plan" in plan_step["run"]
    assert plan_step["run"].startswith("set -euo pipefail\n")
    examples = jobs["examples"]
    assert examples["needs"] == "plan"
    assert examples["strategy"]["fail-fast"] is False
    assert (
        examples["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    )


def test_the_deadline_is_exported_from_the_job_s_own_start_and_timeout():
    """PE C7: the timeout the runner is told is the job's timeout-minutes
    expression, textually; the start is the job's first step."""
    examples = _jobs()["examples"]
    first = examples["steps"][0]
    assert first["run"] == 'echo "DOCEX_JOB_START=$(date +%s)" >> "$GITHUB_ENV"'
    (run,) = [s for s in examples["steps"] if "--run" in s.get("run", "")]
    assert run["env"]["DOCEX_JOB_TIMEOUT_MINUTES"] == examples["timeout-minutes"]
    assert run["env"]["PYTHONUNBUFFERED"] == "1"
    assert run["run"] == (
        'python scripts/doc_examples.py --run --shard "$SHARD" --report "$REPORT"'
    )
    assert run["env"]["SHARD"] == (
        "${{ matrix.profile }}:${{ matrix.shard }}/${{ matrix.of }}"
    )


def test_every_shard_uploads_its_report_and_the_summary_reads_them_all():
    jobs = _jobs()
    (upload,) = [
        s
        for s in jobs["examples"]["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert upload["if"] == "always()"
    assert upload["with"]["overwrite"] is True
    assert upload["with"]["name"].startswith("doc-examples-report-")
    run_env = [s for s in jobs["examples"]["steps"] if "--run" in s.get("run", "")][0]
    assert run_env["env"]["REPORT"].startswith(upload["with"]["path"])
    summary = jobs["summary"]
    assert summary["if"] == "${{ !cancelled() }}"
    assert set(summary["needs"]) == {"plan", "examples"}
    (download,) = [
        s
        for s in summary["steps"]
        if s.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert download["with"]["pattern"] == "doc-examples-report-*"
    assert download["with"]["merge-multiple"] is True
    (check_step,) = [s for s in summary["steps"] if "--summarise" in s.get("run", "")]
    assert check_step["run"] == (
        f"python scripts/doc_examples.py --summarise {download['with']['path']}"
    )
    assert "if" not in check_step, "the summary's check runs even with no download"


def test_no_path_filter_can_skip_the_summary():
    """`paths:` is workflow-level: one on pull_request would drop the summary
    together with the shards it exists to count (PE C6)."""
    triggers = yaml.safe_load(DOC_EXAMPLES_WORKFLOW.read_text())[True]
    pull_request = triggers["pull_request"] or {}
    assert "paths" not in pull_request and "paths-ignore" not in pull_request


# --- timing: the one command that gates on latency or throughput (#1075) ----

GATE = "python backend/tests/performance/run_load_tests.py --users 50 --duration 60s"


def _skip(body: str, reason: str = "timing: gates on latency") -> str:
    return f'{FENCE}{{.bash skip reason="{reason}"}}\n{body}\n{FENCE}'


@pytest.mark.parametrize(
    "body, problem",
    [
        (GATE, None),
        (
            "python backend/tests/performance/run_load_tests.py \\\n"
            "    --users 50 \\\n    --duration 60s",
            None,
        ),
        (f"cd backend && {GATE}", "holds exactly one command"),
        (f"{GATE}\nprintf done", "holds exactly one command"),
        (f"{GATE} > /tmp/out.txt", "holds exactly one command"),
        (f"{GATE} --sla report", "is for a command that gates"),
        (f"{GATE} --sla=report", "is for a command that gates"),
        ("curl -s localhost:8000/health", "is for a command that gates"),
        ("locust -f x.py --headless --run-time 60s", "is for a command that gates"),
    ],
    ids=[
        "the-gate",
        "the-gate-continued",
        "another-command-before",
        "another-command-after",
        "a-redirection",
        "the-report-run",
        "the-report-run-equals",
        "no-gate-at-all",
        "locust-itself",
    ],
)
def test_a_timing_skip_holds_only_the_command_that_gates(body, problem):
    (block,) = blocks(page(_skip(body)))
    found = dx.block_problems(block, "p.md")
    if problem is None:
        assert found == []
    else:
        assert any(problem in p for p in found), found


def test_a_timing_skip_needs_the_same_command_run_with_sla_report():
    report = f"{FENCE}{{.bash exec}}\n{GATE} \\\n    --sla report\n{FENCE}"
    other = f"{FENCE}{{.bash exec}}\n{GATE.replace('50', '10')} --sla report\n{FENCE}"
    assert dx._timing_runs(blocks(page(_skip(GATE), report)), "p.md") == []
    for text in (page(_skip(GATE)), page(_skip(GATE), other)):
        (problem,) = dx._timing_runs(blocks(text), "p.md")
        assert problem.startswith("p.md:3: a 'timing' skip needs the same command")


def test_the_check_applies_the_timing_rules(repo):
    (repo / "docs" / "p.md").write_text(page(_skip(f"cd x && {GATE}")))
    toml = enrol(
        repo,
        """
        [meta]
        documents = 1
        exec = 0
        universe = ["docs/p.md"]

        [[document]]
        path = "docs/p.md"
        environment = "bare"
        exec = 0
        skip = 1
        expects = 0
        """,
    )
    problems = problems_of(toml)
    assert any("holds exactly one command" in p for p in problems), problems
    assert any("needs the same command with --sla report" in p for p in problems)


# --- stack-dev: the Quick Start's stack and a development venv (#1075) ------


def test_stack_dev_pages_run_in_dev_shards_of_their_own():
    documents = [
        _doc("docs/a.md"),
        _doc("docs/d1.md", "stack-dev"),
        _doc("docs/d2.md", "stack-dev"),
        _doc("docs/f.md", "stack-full"),
    ]
    plan = _assert_partition(documents)
    assert plan[("dev", 1, 1)] == ["docs/d1.md", "docs/d2.md"]
    assert plan[("core", 1, 1)] == ["docs/a.md"]
    assert dx.parse_shard("dev:1/1") == ("dev", 1, 1)


def test_a_stack_dev_page_needs_the_quick_start_s_one_start(repo):
    getting = repo / "docs" / "getting-started"
    getting.mkdir()
    (getting / "quick-start.md").write_text(
        page(f"{FENCE}{{.bash exec}}\n{dx.STACK_UP}\n{FENCE}")
    )
    body = textwrap.dedent(
        """
        [meta]
        documents = 2
        exec = 2
        universe = ["docs/getting-started/quick-start.md", "docs/p.md"]

        [[document]]
        path = "docs/p.md"
        environment = "stack-dev"
        exec = 1
        skip = 1
        expects = 1

        [[document]]
        path = "docs/getting-started/quick-start.md"
        environment = "{env}"
        exec = 1
        skip = 0
        expects = 0
        """
    )
    dx.check(enrol(repo, body.format(env="bare")))
    assert any(
        "a 'stack-dev' document needs docs/getting-started/quick-start.md "
        "enrolled as 'bare'" in p
        for p in problems_of(enrol(repo, body.format(env="stack")))
    )


def _python(root: pathlib.Path, script: str) -> None:
    """A stand-in for venv/bin/python under *root*."""
    python = root / dx.DEV_VENV / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(f"#!/bin/sh\n{script}\n")
    python.chmod(0o755)


@pytest.mark.parametrize(
    "script, problem",
    [
        (None, "the development virtual environment is missing"),
        (
            "printf '%s\\n' \"ModuleNotFoundError: No module named 'locust'\" >&2; exit 1",
            "cannot run the page: `venv/bin/python -c 'import alembic, locust, "
            "backend.app.core.config'` exited 1: ModuleNotFoundError: No module "
            "named 'locust'",
        ),
        ("exit 0", None),
    ],
    ids=["no-venv", "a-venv-without-locust", "a-venv-that-imports"],
)
def test_a_stack_dev_page_needs_the_dev_venv_before_its_stack_starts(
    tmp_path, monkeypatch, script, problem
):
    """The venv check runs for real (a stand-in python); the stack start is
    only recorded, and must not happen for a page that cannot run."""
    monkeypatch.setattr(dx, "ROOT", tmp_path)
    if script is not None:
        _python(tmp_path, script)
    monkeypatch.setattr(dx, "preflight", lambda env: [])
    monkeypatch.setattr(dx, "project_resources", lambda project: [])
    run_bounded, started, teardowns = dx.run_bounded, [], []

    def start(argv, env, timeout, deadline=None):
        if argv[0] == str(tmp_path / dx.DEV_VENV / "bin" / "python"):
            return run_bounded(argv, env, timeout, deadline)
        started.append(argv)
        return 0, "", "", None

    def docker(*args, env=None, timeout=120):
        teardowns.append(args)
        return dx.subprocess.CompletedProcess(["docker", *args], 0, "", "")

    monkeypatch.setattr(dx, "run_bounded", start)
    monkeypatch.setattr(dx, "_docker", docker)
    doc = {"path": "p.md", "environment": "stack-dev", "exec": 1}
    problems, reached = dx.run_document(doc, [], 0, {}, None, io.StringIO())
    assert teardowns == [("compose", "down", "-v", "--remove-orphans")]
    if problem is None:
        assert problems == [] and len(started) == 1, (problems, started)
        assert started[0][-1] == dx.STACK_UP
    else:
        assert started == [], "the stack started for a page that cannot run"
        assert reached == 0
        (found,) = problems
        assert found.startswith("p.md: ") and problem in found, found
        assert dx.DEV_VENV_BUILD in found


def test_the_dev_shards_build_the_venv_the_runner_checks():
    """The workflow's venv step is DEV_VENV_BUILD, runs in the dev shards only,
    and the dev shards get the core images pre-built like the core shards."""
    steps = _jobs()["examples"]["steps"]
    (build,) = [
        s for s in steps if s.get("name") == "Build the development virtual environment"
    ]
    assert build["if"] == "matrix.profile == 'dev'"
    assert build["run"].strip().split("\n") == dx.DEV_VENV_BUILD.split(" && ")
    (python,) = [
        s for s in steps if s.get("uses", "").startswith("actions/setup-python@")
    ]
    assert python["with"]["cache"] == "${{ matrix.profile == 'dev' && 'pip' || '' }}"
    (core,) = [s for s in steps if s.get("name", "").endswith("would build (core)")]
    assert core["if"] == (
        "github.event_name == 'pull_request' && "
        "(matrix.profile == 'core' || matrix.profile == 'dev')"
    )
    assert steps.index(build) < [s.get("run", "") for s in steps].index(
        'python scripts/doc_examples.py --run --shard "$SHARD" --report "$REPORT"'
    )


# --- the stack's ports are free before the next page (#1075) -----------------


def _listener() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


def _bare_page(monkeypatch, ports):
    monkeypatch.setattr(dx, "preflight", lambda env: list(ports))
    monkeypatch.setattr(dx, "project_resources", lambda project: [])
    monkeypatch.setattr(
        dx,
        "_docker",
        lambda *args, env=None, timeout=120: dx.subprocess.CompletedProcess(
            ["docker", *args], 0, "", ""
        ),
    )
    return {"path": "p.md", "environment": "bare", "exec": 0}


def _hold(seconds: float) -> tuple[int, subprocess.Popen]:
    """A free port, and another process listening on it for *seconds*.

    A process, not a thread of this one: when a pytest run also collects a
    locustfile (a bare `pytest` does), gevent patches this process, and a
    thread would not run while the runner sleeps in its wait.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    code = (
        "import socket, sys, time\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "s.listen(1)\n"
        "print('listening', flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(port), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "listening"
    return port, holder


def test_the_teardown_waits_until_the_stack_s_ports_are_free(monkeypatch):
    """A port the stack published that is still held after `compose down`
    (a connection in TIME_WAIT on it, say) is waited for, so the next page's
    preflight finds it free, rather than refusing and ending the shard."""
    port, holder = _hold(1.0)
    try:
        doc = _bare_page(monkeypatch, [port])
        out = io.StringIO()
        problems, _ = dx.run_document(doc, [], 0, {}, None, out)
        assert not dx._port_in_use(port), "run_document returned with the port held"
    finally:
        holder.kill()
        holder.wait()
    assert problems == []
    printed = out.getvalue()
    assert f"p.md: port(s) [{port}] still in use after teardown ({port}: " in printed
    assert re.search(
        rf"p\.md: port\(s\) \[{port}\] free [0-9.]+s after teardown", printed
    ), printed


def test_a_port_held_past_the_wait_fails_the_page_naming_what_holds_it(monkeypatch):
    server = _listener()
    port = server.getsockname()[1]
    monkeypatch.setattr(dx, "PORT_RELEASE_SECONDS", 1)
    try:
        doc = _bare_page(monkeypatch, [port])
        problems, _ = dx.run_document(doc, [], 0, {}, None, io.StringIO())
    finally:
        server.close()
    (problem,) = problems
    assert problem.startswith(
        f"p.md: port(s) [{port}] still in use 1s after teardown, so the next page "
        "could not start its stack; they were held by: "
    ), problem


def test_past_the_run_deadline_the_ports_are_not_waited_for(monkeypatch):
    server = _listener()
    port = server.getsockname()[1]
    try:
        reached = dx.Deadline(at=0.0, clock=lambda: 1.0)
        assert dx.release_ports("p.md", [port], reached, io.StringIO()) is None
    finally:
        server.close()


def test_the_wait_polls_until_every_port_is_free():
    now = [0.0]
    held = {1: 2.0, 2: 3.5}  # port -> when it frees

    def sleep(seconds):
        now[0] += seconds

    busy, waited = dx.wait_for_ports(
        [1, 2], 75, lambda p: now[0] < held[p], lambda: now[0], sleep
    )
    assert (busy, waited) == ([], 3.5)
    now[0] = 0.0
    busy, waited = dx.wait_for_ports(
        [1, 2], 3, lambda p: now[0] < held[p], lambda: now[0], sleep
    )
    assert (busy, waited) == ([2], 3.0)


def test_a_busy_port_at_preflight_is_refused_naming_what_holds_it(monkeypatch):
    server = _listener()
    port = server.getsockname()[1]
    monkeypatch.setattr(
        dx,
        "_docker",
        lambda *args, env=None, timeout=120: dx.subprocess.CompletedProcess(
            ["docker", *args], 0, "", ""
        ),
    )
    monkeypatch.setattr(dx, "_published_ports", lambda env: [port])
    try:
        with pytest.raises(dx.Refused) as refusal:
            dx.preflight({})
    finally:
        server.close()
    assert f"port(s) [{port}] are in use" in str(refusal.value)
    assert f"Held by: {port}: " in str(refusal.value)


class _KeepAlive(http.server.BaseHTTPRequestHandler):
    """HTTP/1.1 as the API speaks it: keeps the connection, unless asked to close."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = json.dumps({"profile": "full"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_the_full_profile_probe_leaves_the_api_s_port_free():
    """#1075: urllib sends `Connection: close`, so the API closed first, and the
    side of a TCP connection that closes first keeps it in TIME_WAIT (60 s on
    Linux) on its own port: the stack's published port. The next page's
    preflight could not bind it and refused, ending the full shard. The probe
    keeps the connection alive and closes it itself."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert dx._get_json(f"http://127.0.0.1:{port}/api/v1/modules") == {
            "profile": "full"
        }
    finally:
        server.shutdown()
        server.server_close()
    busy, _ = dx.wait_for_ports([port], 2)
    assert busy == [], (
        "the API's port is still held after the probe and the server are done"
    )


# ---------------------------------------------------------------------------
# The authoring page's rules, each driven through the check (#1142, D73 TR3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "info, timeout",
    [
        ("{.bash exec}", 120),
        ("{.bash exec timeout=1}", 1),
        ("{.bash exec timeout=1200}", 1200),
        ("{.bash exec timeout=3600}", 3600),
    ],
)
def test_an_exec_timeout_is_1_to_3600_seconds_and_120_when_not_given(info, timeout):
    (block,) = blocks(page(f"{FENCE}{info}\nprintf hi\n{FENCE}\n<!-- expect: hi -->"))
    assert block.timeout == timeout
    for refused_value in (0, 3601):
        text = page(
            f"{FENCE}{{.bash exec timeout={refused_value}}}\nprintf hi\n{FENCE}"
        )
        assert "timeout must be 1-3600" in refused(text)


@pytest.mark.parametrize("start", [dx.STACK_UP, dx.STACK_FULL_UP])
def test_both_stack_starts_may_print_without_an_expectation(start):
    assert start in (
        "docker compose up -d --wait",
        "EXPERIMENTLY_PROFILE=full docker compose up -d --wait",
    )
    printed = dx.Outcome(
        reached=True, rc=0, flags="ehuB", pipefail="pf", stdout="#1 build\n"
    )
    assert (
        dx.judge([dx.Block(3, "exec", "bash", start)], [printed], None, 0, "p.md") == []
    )
    other = dx.Block(3, "exec", "bash", start + " --build")
    assert "no expectation" in dx.judge([other], [printed], None, 0, "p.md")[0]


def test_a_page_runs_with_only_path_home_user_tmpdir_and_dockers_settings(
    monkeypatch,
):
    names = {
        "PATH": "/bin",
        "HOME": "/var/empty",
        "USER": "x",
        "TMPDIR": "/tmp",
        "DOCKER_HOST": "unix:///d.sock",
        "DOCKER_CONTEXT": "c",
        "DOCKER_CONFIG": "/d",
        "DOCKER_CERT_PATH": "/c",
        "DOCKER_TLS_VERIFY": "1",
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": "/venv",
        "POSTGRES_PASSWORD": "from-the-caller",
        "AWS_PROFILE": "callers",
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    env = dx.scrubbed_env("docex-unit", {})
    passed = {k for k in env if k in names}
    assert passed == {"PATH", "HOME", "USER", "TMPDIR"} | {
        n for n in names if n.startswith("DOCKER_")
    }
    # And what the runner sets itself: the page's compose project, no .env
    # file, and AWS configuration files that are empty.
    set_here = set(env) - passed
    compose = {"COMPOSE_PROJECT_NAME", "COMPOSE_DISABLE_ENV_FILE"}
    assert compose <= set_here, set_here
    assert env["COMPOSE_PROJECT_NAME"] == "docex-unit"
    aws = set_here - compose
    assert len(aws) == 2 and all(name.startswith("AWS_") for name in aws), aws
    assert {env[name] for name in aws} == {os.devnull}


@pytest.mark.parametrize("keyword", ["export", "declare", "local", "readonly"])
def test_a_masked_substitution_is_refused_for_each_keyword(keyword):
    text = page(f"{FENCE}{{.bash exec}}\n{keyword} X=$(false)\n{FENCE}")
    assert "masks the substitution" in refused(text)
    (ok,) = blocks(page(f"{FENCE}{{.bash exec}}\nX=$(true)\n{keyword} X\n{FENCE}"))
    assert dx.block_problems(ok, "p.md") == []


@pytest.mark.parametrize(
    "body, tool",
    [
        ("pip install x", "pip"),
        ("npm ci", "npm"),
        ("yarn add x", "yarn"),
        ("go get example.com/x", "go get"),
        ("cargo install x", "cargo install"),
    ],
)
def test_the_package_managers_the_page_names_are_refused(body, tool):
    assert f"may not run '{tool}'" in refused(
        page(f"{FENCE}{{.bash exec}}\n{body}\n{FENCE}")
    )


#: The skip-reason categories the authoring page's table lists, in its order.
AUTHORING_CATEGORIES = [
    "checkout",
    "dev",
    "server",
    "aws",
    "idp",
    "secret",
    "registry",
    "toolchain",
    "destructive",
    "demo",
    "fragment",
    "timing",
]


@pytest.mark.parametrize("category", AUTHORING_CATEGORIES)
def test_every_category_the_authoring_page_lists_is_accepted(category):
    body = {
        "registry": "pip install experimently",
        "timing": GATE,
    }.get(category, "printf hi")
    (block,) = blocks(
        page(f'{FENCE}{{.bash skip reason="{category}: the words"}}\n{body}\n{FENCE}')
    )
    assert dx.category_of(block.reason) == category
    assert dx.block_problems(block, "p.md") == []
    (bug,) = blocks(
        page(f'{FENCE}{{.bash skip reason="bug #123: x"}}\nprintf x\n{FENCE}')
    )
    assert dx.category_of(bug.reason) == "bug"
    assert set(dx.CATEGORIES) == set(AUTHORING_CATEGORIES) | {"bug"}


def test_the_check_prints_how_many_skipped_blocks_each_category_holds(repo, capsys):
    write(
        repo,
        "docs/p.md",
        page(
            f"{FENCE}{{.bash exec}}\nprintf hi\n{FENCE}\n<!-- expect: hi -->",
            f'{FENCE}{{.bash skip reason="dev: x"}}\nprintf no\n{FENCE}',
            f'{FENCE}{{.bash skip reason="dev: y"}}\nprintf no\n{FENCE}',
            f'{FENCE}{{.bash skip reason="server: z"}}\nprintf no\n{FENCE}',
        ),
    )
    dx.check(enrol(repo, GOOD.replace("skip = 1", "skip = 3")))
    out = capsys.readouterr().out
    assert "exec blocks: 1; skip blocks by category: dev 2, server 1" in out, out


def test_the_check_prints_the_counts_it_finds(repo, capsys):
    dx.check(enrol(repo, GOOD))
    assert "docs/p.md: 1 exec, 1 skip, 1 expects (bare)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "what, ceiling, worse",
    [
        ("unlabelled", (0, 1, 0), "\n```\nmore\n```\n"),
        ("syntax", (0, 1, 1), "\n```bash\nprintf 'y\n```\n"),
    ],
)
def test_each_pending_residual_can_only_fall(
    pending_repo, monkeypatch, what, ceiling, worse
):
    """The comment residual is test_a_pending_residual_can_only_fall's; the
    other two are counted the same way."""
    text = "# Ops\n\n```bash\naws s3 ls\n```\n\n```\nout\n```\n"
    if what == "syntax":
        text += "\n```bash\nprintf 'x\n```\n"
    write(pending_repo, "docs/ops.md", text)
    monkeypatch.setattr(dx, "PENDING_FROZEN", {"docs/ops.md": ceiling})
    entry = f'"docs/ops.md" = {{ comments = 0, unlabelled = 1, syntax = {ceiling[2]} }}'
    body = WITH_PENDING.replace(
        '"docs/ops.md" = { comments = 1, unlabelled = 1, syntax = 0 }', entry
    )
    dx.check(enrol(pending_repo, body))
    write(pending_repo, "docs/ops.md", text + worse)
    raised = body.replace(f"{what} = 1", f"{what} = 2")
    found = problems_of(enrol(pending_repo, raised))
    assert any("the residual can only fall" in p for p in found), found


def test_an_entry_has_exactly_the_five_keys(repo):
    for body in (
        GOOD.replace("    expects = 1\n", ""),
        GOOD.replace("    expects = 1\n", '    expects = 1\n    title = "x"\n'),
    ):
        with pytest.raises(dx.Refused) as excinfo:
            dx.load(enrol(repo, body))
        assert "want ['environment', 'exec', 'expects', 'path', 'skip']" in str(
            excinfo.value
        )
    dx.load(enrol(repo, GOOD))


def test_an_exempt_reason_is_1_to_200_characters(repo):
    write(repo, "docs/ops.md", "# Ops\n\n```bash\naws s3 ls\n```\n")
    base = GOOD.replace(
        'universe = ["docs/p.md"]', 'universe = ["docs/ops.md", "docs/p.md"]'
    )

    def with_reason(reason: str) -> pathlib.Path:
        return enrol(
            repo,
            base
            + f'\n    [[exempt]]\n    path = "docs/ops.md"\n    reason = "{reason}"\n',
        )

    dx.check(with_reason("x"))
    dx.check(with_reason("r" * 200))
    for reason in ("", "r" * 201):
        assert any(
            "exempt needs a reason (1-200 characters)" in p
            for p in problems_of(with_reason(reason))
        )


def test_a_malformed_shell_fence_on_an_enrolled_page_names_the_file_and_line(repo):
    write(
        repo,
        "docs/p.md",
        page(
            f"{FENCE}{{.bash exec}}\nprintf hi\n{FENCE}\n<!-- expect: hi -->",
            f'{FENCE}{{.bash skip reason="dev: x"}}\nprintf no\n{FENCE}',
            f"{FENCE}bash\nprintf untagged\n{FENCE}",
        ),
    )
    found = problems_of(enrol(repo, GOOD))
    line = (repo / "docs/p.md").read_text().split("\n").index(f"{FENCE}bash") + 1
    assert any(
        p.startswith(f"docs/p.md:{line}: ") and "untagged" in p for p in found
    ), found


def test_only_runs_one_page(repo, no_docker):
    toml = _shard_repo(repo, 3)
    fake = FakePages()
    dx.run(toml, only="docs/q01.md", run_page=fake)
    assert fake.ran == ["docs/q01.md"]
    with pytest.raises(dx.Refused, match="is not enrolled"):
        dx.run(toml, only="docs/nowhere.md", run_page=fake)


@pytest.mark.parametrize(
    "environment, start",
    [
        ("bare", None),
        ("stack", dx.STACK_UP),
        ("stack-full", dx.STACK_FULL_UP),
    ],
)
def test_each_environment_starts_what_it_needs_before_the_page(
    monkeypatch, environment, start
):
    """bare starts nothing; stack runs the Quick Start's start first; and
    stack-full the full profile's, then asks GET /api/v1/modules for the profile."""
    started, probed = [], []
    monkeypatch.setattr(dx, "preflight", lambda env: [])
    monkeypatch.setattr(dx, "project_resources", lambda project: [])
    monkeypatch.setattr(
        dx,
        "_docker",
        lambda *a, env=None, timeout=120: dx.subprocess.CompletedProcess(a, 0, "", ""),
    )

    def run_bounded(argv, env, timeout, deadline=None):
        started.append(argv[-1])
        return 0, "", "", None

    def profile(rel, fetch=None):
        probed.append(rel)
        return

    monkeypatch.setattr(dx, "run_bounded", run_bounded)
    monkeypatch.setattr(dx, "full_profile_problem", profile)
    monkeypatch.setattr(dx, "execute_counted", lambda b, rel, env, d=None: ([], 1))
    doc = {"path": "p.md", "environment": environment, "exec": 1}
    problems, reached = dx.run_document(doc, [], 0, {}, None, io.StringIO())
    assert (problems, reached) == ([], 1)
    assert started == ([start] if start else [])
    assert probed == (["p.md"] if environment == "stack-full" else [])


def test_the_workflow_runs_the_check_and_the_examples_on_every_pull_request():
    workflow = yaml.safe_load(DOC_EXAMPLES_WORKFLOW.read_text())
    triggers = workflow[True]  # PyYAML reads the key `on` as True
    # No filter of any kind (paths, paths-ignore, branches, types): every pull
    # request, whatever it changes.
    assert "pull_request" in triggers
    assert triggers["pull_request"] in (None, {}), triggers["pull_request"]
    jobs = workflow["jobs"]
    for job, command in (
        ("render", "python scripts/doc_examples.py --check"),
        ("examples", "python scripts/doc_examples.py --run --shard"),
    ):
        # Neither job, nor any job it waits for, is conditional.
        waits, seen = [job], set()
        while waits:
            name = waits.pop()
            if name in seen:
                continue
            seen.add(name)
            assert "if" not in jobs[name], name
            needs = jobs[name].get("needs") or []
            waits += [needs] if isinstance(needs, str) else list(needs)
        runs = [s.get("run", "") for s in jobs[job]["steps"]]
        assert any(command in run for run in runs), (job, command)


def test_each_page_runs_under_a_compose_project_of_its_own(monkeypatch):
    projects = []
    monkeypatch.setattr(dx, "preflight", lambda env: [])
    monkeypatch.setattr(dx, "project_resources", lambda project: [])
    monkeypatch.setattr(
        dx,
        "_docker",
        lambda *a, env=None, timeout=120: (
            projects.append(("down", env["COMPOSE_PROJECT_NAME"]))
            or dx.subprocess.CompletedProcess(a, 0, "", "")
        ),
    )

    def execute(blocks, rel, env, deadline=None):
        projects.append(("run", env["COMPOSE_PROJECT_NAME"]))
        return [], 1

    monkeypatch.setattr(dx, "execute_counted", execute)
    for index in (0, 1):
        doc = {"path": f"p{index}.md", "environment": "bare", "exec": 1}
        dx.run_document(doc, [], index, {}, None, io.StringIO())
    (run_a, down_a, run_b, down_b) = projects
    assert run_a[1] == down_a[1] and run_b[1] == down_b[1]
    assert run_a[1] != run_b[1]
    assert all(name.startswith("docex-") for _, name in projects)
