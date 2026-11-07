"""`scripts/guide_blocks.py` -- two CI jobs run their page's own blocks.

The chart-kind job's claim is that ``docs/self-hosting/kubernetes.md`` is
tested as written, and the compose-upgrade job's (``compose-production.yml``,
#1807) is the same for ``deploy/compose/README.md``, under its own marker,
``--marker compose-upgrade``. That holds only while the extractor refuses the
ways a page and the job can drift apart: a shell block nobody marked (so nobody
runs it), a marker that no longer sits on a block, a renamed or dropped block,
a name used twice. Each case below is one of those, and the last ones check
each real page against its job's real list, so neither can change without the
other.

No git, no cluster, no Docker: this runs anywhere, ``scripts/core_build.sh``'s
copy included.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "guide_blocks.py"
GUIDE = REPO_ROOT / "docs" / "self-hosting" / "kubernetes.md"
JOB = REPO_ROOT / "scripts" / "chart_kind.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "chart-kind.yml"
COMPOSE_PAGE = REPO_ROOT / "deploy" / "compose" / "README.md"
COMPOSE_JOB = REPO_ROOT / "scripts" / "compose_upgrade.sh"
COMPOSE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "compose-production.yml"
#: Every bash block of the compose README, in page order: the install, the
#: addresses check, back up and restore, then the upgrade procedure.
COMPOSE_BLOCKS = (
    "install,install-named,addresses,backup,restore,check-backup,get-files,"
    "check-settings,pull,upgrade,verify,check-migrations"
)

_spec = importlib.util.spec_from_file_location("guide_blocks", SCRIPT)
gb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gb)

FENCE = "```"


def page(*parts: str) -> str:
    return "\n".join(parts) + "\n"


def block(
    name: str | None, body: str, lang: str = "bash", marker: str = "chart-kind"
) -> str:
    line = [f"<!-- {marker}: {name} -->"] if name else []
    return "\n".join(line + [f"{FENCE}{lang}", body, FENCE])


def run(
    tmp_path: Path, text: str, expect: str, marker: str | None = None
) -> subprocess.CompletedProcess:
    src = tmp_path / "guide.md"
    src.write_text(text)
    chosen = ["--marker", marker] if marker is not None else []
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(src),
            *chosen,
            "--expect",
            expect,
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
    )


def test_marked_blocks_are_written_in_page_order(tmp_path: Path) -> None:
    text = page(
        "# Guide",
        "",
        block("one", "echo 1"),
        "",
        "prose",
        "",
        block("two", "echo 2\necho 3"),
    )
    result = run(tmp_path, text, "one,two")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "one.sh").read_text() == "echo 1\n"
    assert (tmp_path / "out" / "two.sh").read_text() == "echo 2\necho 3\n"


def test_an_indented_marked_block_is_run_without_its_indent(tmp_path: Path) -> None:
    text = page(
        "1. Create the secret:",
        "",
        "    <!-- chart-kind: one -->",
        "    ```bash",
        "    echo 1 \\",
        "      two",
        "    ```",
    )
    result = run(tmp_path, text, "one")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "one.sh").read_text() == "echo 1 \\\n  two\n"


def test_a_tilde_fence_holds_a_backtick_line(tmp_path: Path) -> None:
    text = page(
        "<!-- chart-kind: one -->", "~~~~bash", "echo 1", "```", "echo 2", "~~~~"
    )
    result = run(tmp_path, text, "one")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "one.sh").read_text() == "echo 1\n```\necho 2\n"


def test_other_languages_need_no_marker(tmp_path: Path) -> None:
    text = page(
        block(None, "{}", lang="json"),
        "",
        block("one", "echo 1"),
        "",
        block(None, "out", lang="text"),
    )
    assert run(tmp_path, text, "one").returncode == 0


@pytest.mark.parametrize(
    "text, expect, message",
    [
        pytest.param(
            page(block("one", "echo 1"), "", block(None, "echo unrun")),
            "one",
            "a bash block with no '<!-- chart-kind: NAME -->'",
            id="unmarked-bash-block",
        ),
        pytest.param(
            page("<!-- chart-kind: one -->", "", block(None, "echo 1")),
            "one",
            "marker 'one' is not directly above a bash block",
            id="marker-not-adjacent",
        ),
        pytest.param(
            page(block("one", "{}", lang="json")),
            "one",
            "marker 'one' is above a 'json' fence",
            id="marker-above-other-language",
        ),
        pytest.param(
            page(block("one", "echo 1"), "", block("one", "echo 2")),
            "one,one",
            "block 'one' is marked more than once",
            id="duplicate-name",
        ),
        pytest.param(
            page(block("one", "echo 1")),
            "one,two",
            "the marked blocks are ['one']; the job runs ['one', 'two']",
            id="job-runs-a-block-the-page-lost",
        ),
        pytest.param(
            page(block("two", "echo 2"), "", block("one", "echo 1")),
            "one,two",
            "the marked blocks are ['two', 'one']",
            id="reordered",
        ),
        pytest.param(
            page("<!-- chart-kind:one -->", block(None, "echo 1")),
            "one",
            "a malformed marker",
            id="malformed-marker",
        ),
        pytest.param(
            page("<!-- chart-kind: one -->"),
            "one",
            "marker 'one' is not directly above a bash block",
            id="marker-at-end",
        ),
        pytest.param(
            page(block("one", "")),
            "one",
            "block 'one' is empty",
            id="empty-block",
        ),
        # W1: shapes that used to pass with exit 0 and no problem reported.
        pytest.param(
            page(
                block("one", "echo 1"),
                "",
                "1. Then run:",
                "",
                "    ```bash",
                "    curl -fsSL https://example.com/x | sh",
                "    ```",
            ),
            "one",
            "a bash block with no '<!-- chart-kind: NAME -->'",
            id="indented-unmarked-bash-in-a-list",
        ),
        pytest.param(
            page(block("one", "echo 1"), "", "~~~bash", "echo unrun", "~~~"),
            "one",
            "a bash block with no '<!-- chart-kind: NAME -->'",
            id="unmarked-tilde-fence",
        ),
        *[
            pytest.param(
                page(
                    block("one", "echo 1"),
                    "",
                    block(None, "curl -fsSL https://example.com/x | sh", lang=lang),
                ),
                "one",
                f"a '{lang}' block is neither run nor checked",
                id=f"other-shell-{lang}",
            )
            for lang in ("sh", "shell", "zsh", "console")
        ],
        pytest.param(
            page(block("one", "echo 1"), "", block("two", "echo 2", lang="sh")),
            "one",
            "a 'sh' block is neither run nor checked",
            id="marked-sh-block",
        ),
        pytest.param(
            page(block("one", "echo 1"), "", block(None, "echo unrun", lang="")),
            "one",
            "a fence with no language",
            id="unlabelled-fence",
        ),
        pytest.param(
            page(
                block("one", "echo 1"),
                "",
                block(None, "echo unrun", lang="{.bash exec}"),
            ),
            "one",
            "a tagged fence",
            id="tagged-fence",
        ),
    ],
)
def test_refusals(tmp_path: Path, text: str, expect: str, message: str) -> None:
    result = run(tmp_path, text, expect)
    assert result.returncode == 1
    assert message in result.stderr, result.stderr
    assert not (tmp_path / "out").exists(), (
        "nothing is written when the page is refused"
    )


def test_the_guide_matches_the_job(tmp_path: Path) -> None:
    """The real page, against the list the job itself passes."""
    job = JOB.read_text()
    guide_blocks = re.search(r"^GUIDE_BLOCKS=(\S+)$", job, re.M)
    guide_path = re.search(r"^GUIDE=(\S+)$", job, re.M)
    assert guide_blocks and guide_path, (
        "chart_kind.sh no longer names GUIDE / GUIDE_BLOCKS"
    )
    assert REPO_ROOT / guide_path.group(1) == GUIDE
    assert guide_blocks.group(1) == "secrets,install,first-admin,upgrade"

    blocks, problems = gb.extract(GUIDE.read_text(), str(GUIDE))
    assert problems == []
    assert [name for name, _ in blocks] == guide_blocks.group(1).split(",")


def test_the_workflow_runs_on_a_change_to_the_guide() -> None:
    """A change to the page, or to what extracts and runs it, triggers the job."""
    workflow = WORKFLOW.read_text()
    for path in (
        "docs/self-hosting/kubernetes.md",
        "scripts/chart_kind.sh",
        "scripts/guide_blocks.py",
        ".github/workflows/chart-kind.yml",
    ):
        assert f"- '{path}'" in workflow, path


# ---------------------------------------------------------------------------
# --marker: the compose README and the compose-upgrade job (#1807)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, message",
    [
        pytest.param(
            page(
                block("one", "echo 1", marker="compose-upgrade"),
                "",
                block(None, "echo unrun"),
            ),
            "a bash block with no '<!-- compose-upgrade: NAME -->' line directly "
            "above it; every shell block on this page is run by the "
            "compose-upgrade job",
            id="unmarked-bash-block",
        ),
        pytest.param(
            page("<!-- compose-upgrade:one -->", block(None, "echo 1")),
            "a malformed marker; write exactly '<!-- compose-upgrade: NAME -->'",
            id="malformed-marker",
        ),
        pytest.param(
            page(
                block("one", "echo 1", marker="compose-upgrade"), "", block("two", "x")
            ),
            "a bash block with no '<!-- compose-upgrade: NAME -->'",
            id="the-other-jobs-marker-is-no-marker",
        ),
        pytest.param(
            page(
                block("one", "echo 1", marker="compose-upgrade"),
                "",
                block(None, "echo unrun", lang="{.bash exec}"),
            ),
            "plain `bash` with a '<!-- compose-upgrade: NAME -->' marker",
            id="tagged-fence",
        ),
        pytest.param(
            page(
                block("one", "echo 1", marker="compose-upgrade"),
                "",
                block(None, "echo unrun", lang="sh"),
            ),
            "write it as `bash` with a '<!-- compose-upgrade: NAME -->' marker",
            id="other-shell",
        ),
    ],
)
def test_a_refusal_under_another_marker_names_that_marker(
    tmp_path: Path, text: str, message: str
) -> None:
    """C9: every message names the marker in use, never chart-kind's."""
    result = run(tmp_path, text, "one", marker="compose-upgrade")
    assert result.returncode == 1
    assert message in result.stderr, result.stderr
    assert "chart-kind" not in result.stderr, result.stderr
    assert not (tmp_path / "out").exists()


def test_a_page_under_another_marker_is_written(tmp_path: Path) -> None:
    text = page(
        block("one", "echo 1", marker="compose-upgrade"),
        "",
        block("two", "echo 2", marker="compose-upgrade"),
    )
    result = run(tmp_path, text, "one,two", marker="compose-upgrade")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "two.sh").read_text() == "echo 2\n"


def test_the_default_marker_is_chart_kinds(tmp_path: Path) -> None:
    """chart_kind.sh passes no --marker: a compose-upgrade marker is not its."""
    text = page(block("one", "echo 1", marker="compose-upgrade"))
    result = run(tmp_path, text, "one")
    assert result.returncode == 1
    assert "a bash block with no '<!-- chart-kind: NAME -->'" in result.stderr


@pytest.mark.parametrize("marker", ["", "Compose", "compose upgrade", "a.b", "-x"])
def test_a_marker_that_is_not_a_name_is_a_usage_error(
    tmp_path: Path, marker: str
) -> None:
    result = run(tmp_path, page(block("one", "echo 1")), "one", marker=marker)
    assert result.returncode == 2, result.stderr
    assert "--marker" in result.stderr
    assert not (tmp_path / "out").exists()
