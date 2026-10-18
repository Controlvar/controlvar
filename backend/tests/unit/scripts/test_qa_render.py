"""The one renderer of the QA templates refuses what it cannot vouch for.

``scripts/qa_render.py`` turns a template from ``.github/qa-templates/`` into
the text a QA workflow posts in public. Pinned here:

* it refuses a placeholder outside its fixed mapping, a placeholder with no
  value and a value for a name the template does not hold;
* every value is checked against its kind with an anchored pattern, and each
  kind has planted bad values that must be refused (a value of the wrong kind
  is the defect this guards against);
* it renders in one pass and its output is ASCII;
* an issue template splits into its title and body;
* it is the only reader of the directory among the repository's scripts and
  workflows.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import qa_render

TEMPLATES_DIR = REPO_ROOT / ".github" / "qa-templates"

#: A valid value of each kind, used to render every real template.
GOOD = {
    "date": "2026-10-06",
    "date_time": "2026-10-06 14:17 UTC",
    "sha": "c6e0f081d82f1a858e111c86db62aa5d8b8a71d4",
    "run_link": "https://github.com/Controlvar/controlvar/actions/runs/37456943504",
    "check": "staging",
    "step": "4",
    "status": "503",
    "seed": "20261006",
    "duration": "2 h 30 min",
    "count": "3",
    "guide_path": "getting-started/quick-start.md",
    "step_number": "12",
    "heading": "Create your first experiment",
}

#: Bad values per kind: each must be refused. Shapes a value could take by
#: mistake or on purpose (a body, a key, a URL elsewhere, a second line).
BAD = {
    "date": ["2026-13-01", "2026-02-30", "26-10-06", "2026-10-06 ", "2026/10/06", ""],
    "date_time": [
        "2026-10-06 24:00 UTC",
        "2026-10-06 14:17",
        "2026-10-06T14:17Z",
        "2026-10-06 14:17 UTC\nx",
    ],
    "sha": [
        "c6e0f0",
        "C6E0F081",
        "c6e0f081d82f1a858e111c86db62aa5d8b8a71d4a",
        "g6e0f081",
        "c6e0f081 ",
        "{sha}",
    ],
    "run_link": [
        "https://github.com/getexperimently/experimently-other/actions/runs/1",
        "https://github.com/other/controlvar/actions/runs/1",
        "http://github.com/Controlvar/controlvar/actions/runs/1",
        "https://github.com/Controlvar/controlvar/actions/runs/1/attempts/2",
        "https://github.com/Controlvar/controlvar/actions/runs/1?x=y",
        "https://github.com/Controlvar/controlvar/actions/runs/",
        "https://github.com/Controlvar/controlvar/actions/runs/0",
        "https://example.com/Controlvar/controlvar/actions/runs/1",
    ],
    "check": ["Staging", "prod", "staging ", "", "staging\nx"],
    "step": ["0", "9", "10", "4 ", "four", "-1"],
    "status": ["5000", "50", "abc", "503 ", "099", "no  answer", "No answer", "503\n"],
    "seed": ["-1", "12345678901", "1e3", "0x10", ""],
    "duration": [
        "2 hours",
        "2 h 30",
        "30 min ",
        "100 min",
        "0 h 5 min",
        "12345 h 0 min",
    ],
    "count": ["-1", "1234567", "1.0", "1e3", "", " 3"],
    "guide_path": [
        "/etc/passwd.md",
        "../secrets.md",
        "guides/../x.md",
        "getting-started/quick-start.html",
        "frontend/src/components/experiments/FirstRunChecklist.ts",
        "Getting Started/x.md",
        "_hidden/x.md",
        "a b.md",
        "x" * 200 + ".md",
    ],
    "step_number": ["0", "1000", "1.5", ""],
    "heading": [
        "Mail me at someone@example.org",
        "<script>",
        "a > b",
        "Costs $5",
        "a {step} b",
        "a } b",
        "see https://example.org",
        "see www.example.org",
        "two\nlines",
        "café",
        "tab\there",
        "",
        " padded",
        "x" * 121,
        "back`tick",
    ],
}

#: One placeholder name of each kind.
NAME_OF_KIND = {
    "date": "date",
    "date_time": "date_time",
    "sha": "sha",
    "run_link": "run_link",
    "check": "check",
    "step": "step",
    "status": "status",
    "seed": "seed",
    "duration": "duration",
    "count": "count_runs",
    "guide_path": "guide_path",
    "step_number": "step_number",
    "heading": "heading_step",
}


def good_value(name: str) -> str:
    return GOOD[qa_render.PLACEHOLDERS[name]]


def values_for(text: str) -> dict:
    return {name: good_value(name) for name in set(qa_render.PLACEHOLDER.findall(text))}


def template_names():
    return sorted(path.name for path in TEMPLATES_DIR.glob("*.tmpl"))


def test_every_kind_has_a_check_good_values_and_planted_bad_ones():
    kinds = set(qa_render.PLACEHOLDERS.values())
    assert kinds == set(qa_render.KINDS) == set(GOOD) == set(BAD) == set(NAME_OF_KIND)
    for kind, name in NAME_OF_KIND.items():
        assert qa_render.PLACEHOLDERS[name] == kind


@pytest.mark.parametrize("kind", sorted(GOOD))
def test_a_good_value_of_each_kind_is_accepted(kind):
    qa_render.check_value(NAME_OF_KIND[kind], GOOD[kind])


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "rbac/README.md",
        "Enhanced_Rules_Engine_Reference.md",
        "frontend/src/components/experiments/FirstRunChecklist.tsx",
    ],
)
def test_a_nav_page_named_with_capitals_is_a_guide_path(path):
    """Three pages of the docs nav have capitals in their names; README.md is
    the docs-site journey's own guide, so its report header names it. The
    first-run journey's guide is the dashboard component it walks."""
    qa_render.check_value("guide_path", path)


@pytest.mark.parametrize(
    "kind, value",
    [(kind, value) for kind, values in sorted(BAD.items()) for value in values],
)
def test_a_value_of_the_wrong_kind_is_refused(kind, value):
    name = NAME_OF_KIND[kind]
    with pytest.raises(qa_render.RenderError) as refused:
        qa_render.check_value(name, value)
    # The refusal names the placeholder and kind, never the value.
    assert name in str(refused.value)
    if value.strip():
        assert value not in str(refused.value)


#: A repository by another name: what a rename or a transfer makes this one.
RENAMED = "example-org/example-repo"


@pytest.mark.regression
def test_a_run_link_names_the_repository_the_run_is_in(monkeypatch):
    """The run link's repository is ``GITHUB_REPOSITORY``, not a written name.

    It used to be this repository's name, written into the script, so after a
    rename every run link the workflows built was refused and no failure was
    reported."""
    monkeypatch.setenv("GITHUB_REPOSITORY", RENAMED)
    qa_render.check_value(
        "run_link", f"https://github.com/{RENAMED}/actions/runs/37456943504"
    )
    with pytest.raises(qa_render.RenderError):
        qa_render.check_value("run_link", GOOD["run_link"])


@pytest.mark.parametrize(
    "repository", [None, "", "owner", "a/b/c", "owner/repo\n", "-owner/repo", "o/r?"]
)
def test_no_run_link_passes_without_a_repository_name(monkeypatch, repository):
    if repository is None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    assert qa_render.run_link_prefix() is None
    with pytest.raises(qa_render.RenderError):
        qa_render.check_value("run_link", GOOD["run_link"])


def test_the_mapping_covers_exactly_the_placeholders_the_templates_use():
    used = set()
    for name in template_names():
        used |= set(qa_render.PLACEHOLDER.findall((TEMPLATES_DIR / name).read_text()))
    assert used <= set(qa_render.PLACEHOLDERS)


@pytest.mark.parametrize("name", template_names())
def test_every_real_template_renders_to_ascii(name):
    text = (TEMPLATES_DIR / name).read_text()
    rendered = qa_render.render(name, values_for(text))
    assert rendered.body.isascii()
    assert "{" not in rendered.body and "}" not in rendered.body
    if name.endswith("-issue.tmpl"):
        assert rendered.title and "\n" not in rendered.title
        assert rendered.title == qa_render.render_title(
            name, values_for(text.split("\n", 1)[0])
        )
    else:
        assert rendered.title is None


def _write(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_an_unknown_placeholder_is_refused(tmp_path):
    directory = _write(tmp_path, "x.tmpl", "Run {run_link}, body {response_body}\n")
    with pytest.raises(
        qa_render.RenderError, match="unknown placeholders.*response_body"
    ):
        qa_render.render("x.tmpl", {"run_link": GOOD["run_link"]}, directory)


def test_a_missing_value_is_refused(tmp_path):
    directory = _write(tmp_path, "x.tmpl", "{check} at {step}\n")
    with pytest.raises(qa_render.RenderError, match="no value for.*step"):
        qa_render.render("x.tmpl", {"check": "staging"}, directory)


def test_an_extra_value_is_refused(tmp_path):
    directory = _write(tmp_path, "x.tmpl", "{check}\n")
    with pytest.raises(qa_render.RenderError, match="does not hold.*status"):
        qa_render.render("x.tmpl", {"check": "staging", "status": "503"}, directory)


def test_rendering_is_a_single_pass():
    """A value that looks like a placeholder is inserted as it is, never
    expanded (the kinds refuse braces anyway; this pins the pass itself)."""
    text = qa_render.substitute("{check} / {step}\n", {"check": "{step}", "step": "4"})
    assert text == "{step} / 4\n"


@pytest.mark.parametrize(
    "template",
    [
        "Run: {Run_link} at {check}\n",
        "{check} {{check}}\n",
        "{check} }\n",
        "{check} {\n",
        "{check} {count runs}\n",
    ],
)
def test_a_brace_left_after_rendering_is_refused(tmp_path, template):
    """Only a lower-case name is a placeholder, so ``{Run_link}`` would pass
    through unrendered; any brace left in the output is refused."""
    directory = _write(tmp_path, "x.tmpl", template)
    with pytest.raises(qa_render.RenderError, match="brace"):
        qa_render.render("x.tmpl", {"check": "staging"}, directory)


def test_a_brace_in_a_value_is_refused_by_the_output_check(tmp_path, monkeypatch):
    """Even if a kind let a brace through, the output check stops it."""
    monkeypatch.setitem(qa_render.KINDS, "check", lambda value: True)
    directory = _write(tmp_path, "x.tmpl", "{check} / {step}\n")
    with pytest.raises(qa_render.RenderError, match="brace"):
        qa_render.render("x.tmpl", {"check": "{step}", "step": "4"}, directory)


def test_non_ascii_output_is_refused(tmp_path):
    directory = _write(tmp_path, "x.tmpl", "café {check}\n")
    with pytest.raises(qa_render.RenderError, match="ASCII"):
        qa_render.render("x.tmpl", {"check": "staging"}, directory)


@pytest.mark.parametrize(
    "name", ["../README.md", "README.md", "x.tmpl/..", "X.tmpl", "a b.tmpl"]
)
def test_only_a_plain_template_name_is_read(name):
    with pytest.raises(qa_render.RenderError):
        qa_render.read_template(name)


def test_an_issue_template_must_be_title_blank_body(tmp_path):
    directory = _write(tmp_path, "x-issue.tmpl", "Title {check}\nbody right away\n")
    with pytest.raises(qa_render.RenderError, match="title, a blank line"):
        qa_render.render("x-issue.tmpl", {"check": "staging"}, directory)


def test_the_command_line_writes_body_and_title(tmp_path):
    sets = [
        f"--set={name}={value}"
        for name, value in values_for(
            (TEMPLATES_DIR / "synthetic-issue.tmpl").read_text()
        ).items()
    ]
    body, title = tmp_path / "body.md", tmp_path / "title.txt"
    code = qa_render.main(
        ["synthetic-issue.tmpl", *sets, "--out", str(body), "--title-out", str(title)]
    )
    assert code == 0
    assert title.read_text() == "Synthetic check (staging) is red\n"
    assert body.read_text().startswith("The staging synthetic check has failed 3 runs")


def test_the_command_line_refuses_a_bad_value_without_printing_it(tmp_path, capsys):
    text = (TEMPLATES_DIR / "synthetic-step-changed.tmpl").read_text()
    values = values_for(text)
    values["status"] = "SENTINEL-not-a-status"
    sets = [f"--set={name}={value}" for name, value in values.items()]
    code = qa_render.main(
        ["synthetic-step-changed.tmpl", *sets, "--out", str(tmp_path / "o")]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "status" in captured.err
    assert "SENTINEL" not in captured.out + captured.err
    assert not (tmp_path / "o").exists()


#: Files that may read .github/qa-templates/: the renderer, and the tests.
READERS = {"scripts/qa_render.py"}


def _code_strings(source: str):
    """Every string constant of a Python file except its docstrings."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def reader_problems(rel: str, text: str):
    """A script other than the renderer that names the directory in code, or a
    shell script that names it or a template file at all."""
    if rel in READERS:
        return []
    if rel.endswith(".py"):
        named = [s for s in _code_strings(text) if "qa-templates" in s]
    else:
        named = re.findall(r"qa-templates|\.tmpl\b|envsubst", text)
    return [rel] if named else []


def test_the_renderer_is_the_only_reader_of_the_templates():
    """No other script reaches the directory (a template name handed to the
    renderer is fine: the renderer reads only from its own directory), and
    no workflow reads, pipes or substitutes a template (a workflow may name
    the directory in a comment or in a sparse checkout)."""
    found = []
    for path in sorted(SCRIPTS.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".sh"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            found += reader_problems(
                rel, path.read_text(encoding="utf-8", errors="replace")
            )
    workflows = [
        *REPO_ROOT.glob(".github/workflows/*.y*ml"),
        *REPO_ROOT.glob(".github/actions/*/action.y*ml"),
    ]
    assert len(workflows) > 20, "the scan found almost nothing: it is broken"
    for path in workflows:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if re.search(r"qa-templates|\.tmpl\b|envsubst", code) and not re.fullmatch(
                r"\s*-?\s*\.github/qa-templates\s*", code
            ):
                found.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not found, f"read the QA templates outside scripts/qa_render.py: {found}"


@pytest.mark.parametrize(
    "rel, text",
    [
        ("scripts/x.py", 'TEMPLATES = Path(".github/qa-templates")\n'),
        ("scripts/x.py", 'open(ROOT / ".github" / "qa-templates" / "fuzz-red.tmpl")\n'),
        ("scripts/x.sh", 'body="$(envsubst < .github/qa-templates/fuzz-red.tmpl)"\n'),
        ("scripts/x.sh", 'sed "s/{seed}/$SEED/" fuzz-red.tmpl > out.md\n'),
    ],
)
def test_the_reader_scan_fires_on_a_planted_reader(rel, text):
    assert reader_problems(rel, text) == [rel]


def test_a_docstring_naming_the_directory_is_not_a_reader():
    text = '"""Rendered from .github/qa-templates/ by qa_render."""\nx = "synthetic-issue.tmpl"\n'
    assert reader_problems("scripts/x.py", text) == []


def test_the_renderer_imports_nothing_from_the_backend():
    tree = ast.parse((SCRIPTS / "qa_render.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
