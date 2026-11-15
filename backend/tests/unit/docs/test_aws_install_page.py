"""The beta AWS installer's page: what only a page can lose.

``docs/self-hosting/aws-install.md`` documents ``deploy/aws-installer/controlvar-aws``.
Three things about it are held here, because nothing else reads them:

* The page says "beta" everywhere it speaks for itself: its title, its entry in
  the navigation, the warning that opens it, its claims registry and its
  inventory entry. The installer's own ``--help`` and first line say it in the
  installer's tests.
* A block on the page that installs, uninstalls or reads the AWS account is a
  block that does not run. Doc Examples cannot tell: its rule for AWS looks for a
  command named ``aws``, and the launcher is not one, so a block that ran the
  installer would reach for the account in CI and be stopped only by the
  no-real-AWS shim. The one block that runs is ``--help``, which needs neither
  an account nor a network.
* The headings that the installer's messages link to are on the page, so a
  message never sends a reader to the top of a long page.

Each check is a function of the file texts, so the planted-defect tests below run
the same code on edited copies, one defect each, and see it name the file.

It lives in backend/tests/unit/docs/, so a pull request that changes docs/ alone
runs it (the "Docs content tests" step collects this directory). Reads files
only; no git, no network, no AWS.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = "docs/self-hosting/aws-install.md"
NAV_PATH = "self-hosting/aws-install.md"
REGISTRY = "docs/_claims/docs/self-hosting/aws-install.md.toml"
INVENTORY = "tests/acceptance/docs/inventory.toml"
MKDOCS = "mkdocs.yml"
DOC_EXAMPLES = REPO_ROOT / "scripts" / "doc_examples.py"

#: The launcher's name, and the one command the page may run.
LAUNCHER = "controlvar-aws"
HELP = "./deploy/aws-installer/controlvar-aws --help"
#: What a block that calls the launcher and does not run may give as its reason.
SKIP_CATEGORIES = ("aws", "destructive")

#: The installer's own files, which hold every link it prints to this page.
INSTALLER_DIR = "deploy/aws-installer"
LAUNCHER_FILE = f"{INSTALLER_DIR}/controlvar-aws"
SWEEP_SH = f"{INSTALLER_DIR}/lib/sweep.sh"
UNINSTALL_SH = f"{INSTALLER_DIR}/lib/uninstall.sh"
NAMES_SH = f"{INSTALLER_DIR}/lib/names.sh"
REDIS_STACK = "infrastructure/cdk/stacks/elasticache_redis_stack.py"

#: A link the installer prints to this page, as docs.controlvar.com serves it:
#: https://docs.controlvar.com/self-hosting/aws-install/#<anchor>. The installer
#: writes it as ${CV_PAGE}#<anchor> (CV_PAGE is the page's address), or through a
#: local that holds CV_PAGE (lib/steps.sh's ``page``), or in full.
_ANCHOR_LINK = re.compile(
    r"(?:\$\{?(?:CV_PAGE|page)\}?|/self-hosting/aws-install/)#([a-z0-9][a-z0-9-]*)"
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


class _AnyTagLoader(yaml.SafeLoader):
    """A safe loader that builds a node with an unknown tag as plain YAML."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_AnyTagLoader.add_multi_constructor("", _untagged)


def _doc_examples() -> Any:
    """scripts/doc_examples.py, loaded by path under a name of its own."""
    name = "doc_examples_for_the_installer_page"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DOC_EXAMPLES)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses look their module up
        spec.loader.exec_module(module)
    return sys.modules[name]


# ---------------------------------------------------------------------------
# The page's headings
# ---------------------------------------------------------------------------
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def headings(page: str) -> List[str]:
    """Each heading's text, in order, leaving out anything inside a fence."""
    found: List[str] = []
    fence = ""
    for line in page.splitlines():
        opener = _FENCE.match(line)
        if opener:
            marker = opener.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if not fence:
            heading = _HEADING.match(line)
            if heading:
                found.append(heading.group(2))
    return found


def slug(heading: str) -> str:
    """The anchor MkDocs' default slugify gives a plain heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text)


def title_of(page: str) -> str:
    for line in page.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ---------------------------------------------------------------------------
# 1. "beta" on every surface the page owns
# ---------------------------------------------------------------------------
def nav_labels(mkdocs: str) -> List[str]:
    """The labels under which the nav lists the page."""
    config = yaml.load(mkdocs, Loader=_AnyTagLoader)
    labels: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for label, value in node.items():
                if value == NAV_PATH:
                    labels.append(str(label))
                else:
                    walk(value)

    walk(config.get("nav", []))
    return labels


def beta_problems(page: str, mkdocs: str, registry: str, inventory: str) -> List[str]:
    """One line for each surface of the page that does not say beta, naming its file."""
    found: List[str] = []
    title = title_of(page)
    if "(beta)" not in title:
        found.append(f"{PAGE}: the title {title!r} does not say (beta)")
    warning = re.search(r'^!!! warning "([^"]*)"', page, re.MULTILINE)
    if warning is None or not warning.group(1).startswith("Beta"):
        found.append(
            f"{PAGE}: the warning that opens the page does not start with Beta"
        )
    labels = nav_labels(mkdocs)
    if not labels:
        found.append(f"{MKDOCS}: the nav has no entry for {NAV_PATH}")
    elif not all("beta" in label.lower() for label in labels):
        found.append(
            f"{MKDOCS}: the nav entry {labels!r} for {NAV_PATH} does not say beta"
        )
    units = tomllib.loads(registry).get("unit", [])
    titled = [u for u in units if str(u.get("text", "")).startswith("Install on AWS")]
    if not titled:
        found.append(f"{REGISTRY}: no unit for the page's title")
    elif "(beta)" not in titled[0]["text"]:
        found.append(
            f"{REGISTRY}: the unit for the title {titled[0]['text']!r} does not say (beta)"
        )
    entry = tomllib.loads(inventory).get("pages", {}).get(NAV_PATH)
    if entry is None:
        found.append(f"{INVENTORY}: no entry for {NAV_PATH}")
    else:
        if entry.get("class") != "aws":
            found.append(
                f"{INVENTORY}: {NAV_PATH} is class {entry.get('class')!r}, not aws"
            )
        if "beta" not in str(entry.get("reason", "")).lower():
            found.append(f"{INVENTORY}: the reason for {NAV_PATH} does not say beta")
    return found


# ---------------------------------------------------------------------------
# 2. The blocks that call the installer
# ---------------------------------------------------------------------------
def block_problems(page: str) -> List[str]:
    """Every block on the page that calls the launcher and is not what it must be."""
    doc_examples = _doc_examples()
    blocks = []
    for fence in doc_examples.parse_fences(page, PAGE):
        block = doc_examples.classify(fence, PAGE)
        if block is not None:
            blocks.append(block)
    found: List[str] = []
    runs = [b for b in blocks if b.kind == "exec"]
    skipped_verbs = set()
    for block in blocks:
        text = doc_examples.shell_text(block.body)
        if LAUNCHER not in text:
            continue
        where = f"{PAGE}:{block.line}"
        if block.kind == "exec":
            if text.strip() != HELP:
                found.append(
                    f"{where}: a block that runs may call the installer only as {HELP!r}"
                )
            continue
        category = doc_examples.category_of(block.reason)
        if category not in SKIP_CATEGORIES:
            found.append(
                f"{where}: a block that calls the installer and does not run says why with"
                f" one of {SKIP_CATEGORIES}, not {category!r}"
            )
        for verb in ("install", "uninstall"):
            if re.search(rf"{LAUNCHER}\s+{verb}\b", text):
                skipped_verbs.add(verb)
    if len(runs) != 1:
        found.append(
            f"{PAGE}: {len(runs)} blocks run; the installer's --help is the only one"
        )
    for verb in ("install", "uninstall"):
        if verb not in skipped_verbs:
            found.append(
                f"{PAGE}: no skipped block runs the installer's {verb}: this check found nothing"
            )
    return found


# ---------------------------------------------------------------------------
# 3. The anchors the installer's messages link to
# ---------------------------------------------------------------------------
def installer_texts() -> Dict[str, str]:
    """The launcher and its libraries, which hold every link the installer prints."""
    paths = [LAUNCHER_FILE] + sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / INSTALLER_DIR / "lib").glob("*.sh")
    )
    return {path: _read(path) for path in paths}


def installer_anchors(texts: Dict[str, str]) -> Set[str]:
    """The anchors of this page that the installer's files link to."""
    return {m.group(1) for text in texts.values() for m in _ANCHOR_LINK.finditer(text)}


def anchor_problems(page: str, texts: Dict[str, str]) -> List[str]:
    anchors = installer_anchors(texts)
    if not anchors:
        return [
            f"{INSTALLER_DIR}: no file links to an anchor of this page: this check found nothing"
        ]
    have = {slug(h) for h in headings(page)}
    return [
        f"{PAGE}: no heading makes the anchor #{anchor}, which the installer's messages link to"
        for anchor in sorted(anchors)
        if anchor not in have
    ]


# ---------------------------------------------------------------------------
# 4. The cache's backup, which the page names by file and by group
# ---------------------------------------------------------------------------
_BACKUP_LINE = re.compile(
    r"us-west-2/(?P<file>[a-z0-9-]+)\.txt \+ automatic\.(?P<group>[a-z0-9-]+)-001-"
    r"<yyyy-mm-dd-hh-mm> automated"
)
BACKUP_COMMAND = "aws elasticache describe-snapshots --region us-west-2"


def backup_problems(page: str, sweep: str, stack: str, names: str) -> List[str]:
    """The uninstall report's line for the cache's automatic backup, as the page quotes
    it, against the files that make it: the sweep that writes the list (its file name),
    and the Redis stack that names the group and sets the backup window."""
    found = _BACKUP_LINE.search(page)
    if found is None:
        return [
            f"{PAGE}: no line of the cache's automatic backup, in the report's form"
        ]
    problems: List[str] = []
    file, group = found.group("file"), found.group("group")
    if (
        re.search(rf"\b{re.escape(file)} elasticache describe-snapshots\b", sweep)
        is None
    ):
        problems.append(
            f"{SWEEP_SH}: no read writes {file}.txt from describe-snapshots, which the page names"
        )
    stack_names = re.findall(r'"(experimentation-redis-[a-z]+)"', names)
    if len(stack_names) != 2 or len(set(stack_names)) != 1:
        problems.append(
            f"{NAMES_SH}: not exactly the Redis stack's name, once in each list"
        )
    elif f"{stack_names[0]}-redis" != group:
        problems.append(
            f"{PAGE}: the group {group!r} is not the Redis stack's name {stack_names[0]!r} and -redis"
        )
    if 'f"{construct_id}-redis"' not in stack:
        problems.append(
            f"{REDIS_STACK}: the group is no longer the stack's id and -redis"
        )
    if 'snapshot_window="02:00-03:00"' not in stack:
        problems.append(f"{REDIS_STACK}: the backup window is no longer 02:00-03:00")
    if "02:00 to 03:00 UTC" not in page:
        problems.append(
            f"{PAGE}: the page no longer names the window, 02:00 to 03:00 UTC"
        )
    if BACKUP_COMMAND not in page:
        problems.append(
            f"{PAGE}: the check for the backup is no longer {BACKUP_COMMAND!r}"
        )
    elif "elasticache describe-snapshots --region" not in sweep:
        problems.append(
            f"{SWEEP_SH}: the sweep no longer reads the cache's backups by region"
        )
    return problems


# ---------------------------------------------------------------------------
# 5. What the installer keeps, in the words it prints
# ---------------------------------------------------------------------------
KEPT_LITERAL = "Kept by the installer: the CDK's setup stack (CDKToolkit), its bucket, repository, parameter and roles, which this install made"
KEEPS_LITERAL = "Keeps: the zone"


def kept_problems(page: str, uninstall: str) -> List[str]:
    """The page's report and plan screens carry the words uninstall prints for what it
    keeps: the CDK's setup stack is kept, and listed as kept."""
    problems: List[str] = []
    for literal in (KEPT_LITERAL, KEEPS_LITERAL):
        if literal not in uninstall:
            problems.append(f"{UNINSTALL_SH}: no longer prints {literal!r}")
        if literal not in page:
            problems.append(f"{PAGE}: the screens no longer show {literal!r}")
    return problems


# ---------------------------------------------------------------------------
# The real files
# ---------------------------------------------------------------------------
def test_the_page_says_beta_wherever_it_speaks_for_itself() -> None:
    found = beta_problems(_read(PAGE), _read(MKDOCS), _read(REGISTRY), _read(INVENTORY))
    assert not found, "\n".join(found)


def test_only_the_help_block_runs_and_every_block_that_installs_is_a_skip() -> None:
    found = block_problems(_read(PAGE))
    assert not found, "\n".join(found)


def test_every_heading_the_installers_messages_link_to_is_on_the_page() -> None:
    found = anchor_problems(_read(PAGE), installer_texts())
    assert not found, "\n".join(found)


def test_the_cache_backup_the_page_names_is_the_one_the_files_make() -> None:
    found = backup_problems(
        _read(PAGE), _read(SWEEP_SH), _read(REDIS_STACK), _read(NAMES_SH)
    )
    assert not found, "\n".join(found)


def test_what_the_installer_keeps_is_said_in_the_words_it_prints() -> None:
    found = kept_problems(_read(PAGE), _read(UNINSTALL_SH))
    assert not found, "\n".join(found)


# ---------------------------------------------------------------------------
# Planted defects: each check is seen to fire, and to name the right file
# ---------------------------------------------------------------------------
TITLE = "# Install on AWS with one command (beta)"
INSTALL_TAG = '```{.bash skip reason="aws: creates the install in your AWS account, which AWS bills"}'
UNINSTALL_TAG = '```{.bash skip reason="destructive: deletes the install and its data"}'


def _plant_page(old: str, new: str) -> str:
    page = _read(PAGE)
    assert page.count(old) == 1, f"the page no longer holds exactly one {old!r}"
    return page.replace(old, new)


def _plant_text(path: str, old: str, new: str) -> str:
    text = _read(path)
    assert text.count(old) == 1, f"{path} no longer holds exactly one {old!r}"
    return text.replace(old, new)


@pytest.mark.parametrize(
    ("surface", "file"),
    [
        ("title", PAGE),
        ("warning", PAGE),
        ("nav", MKDOCS),
        ("registry", REGISTRY),
        ("inventory", INVENTORY),
    ],
)
def test_dropping_beta_from_one_surface_names_its_file(surface: str, file: str) -> None:
    page, mkdocs = _read(PAGE), _read(MKDOCS)
    registry, inventory = _read(REGISTRY), _read(INVENTORY)
    if surface == "title":
        page = _plant_page(TITLE, "# Install on AWS with one command")
    elif surface == "warning":
        page = _plant_page('!!! warning "Beta: ', '!!! warning "Note: ')
    elif surface == "nav":
        mkdocs = _plant_text(MKDOCS, "AWS installer (beta):", "AWS installer:")
    elif surface == "registry":
        registry = _plant_text(
            REGISTRY,
            'text = "Install on AWS with one command (beta)"',
            'text = "Install on AWS with one command"',
        )
    else:
        inventory = _plant_text(
            INVENTORY, "the beta AWS installer:", "the AWS installer:"
        )
    found = beta_problems(page, mkdocs, registry, inventory)
    assert len(found) == 1, found
    assert found[0].startswith(file + ":"), found


def test_an_install_block_that_runs_is_named_by_its_line() -> None:
    page = _plant_page(INSTALL_TAG, "```{.bash exec}")
    found = block_problems(page)
    assert any("may call the installer only as" in line for line in found), found
    line_number = (
        page[
            : page.index(
                "```{.bash exec}\n./deploy/aws-installer/controlvar-aws install "
            )
        ].count("\n")
        + 1
    )
    assert any(line.startswith(f"{PAGE}:{line_number}:") for line in found), found


def test_a_second_block_that_runs_is_refused() -> None:
    page = _read(PAGE) + (
        "\n```{.bash exec}\n./deploy/aws-installer/controlvar-aws install --plan-only\n```\n"
        "<!-- expect: ok -->\n"
    )
    found = block_problems(page)
    assert any("may call the installer only as" in line for line in found), found
    assert any("2 blocks run" in line for line in found), found


def test_an_uninstall_block_with_another_reason_is_refused() -> None:
    page = _plant_page(
        UNINSTALL_TAG, '```{.bash skip reason="dev: deletes the install and its data"}'
    )
    found = block_problems(page)
    assert len(found) == 1 and "'dev'" in found[0], found


def test_a_page_with_no_installer_block_does_not_pass_for_having_none() -> None:
    page = "# Install on AWS with one command (beta)\n\nNothing here.\n"
    found = block_problems(page)
    assert any("found nothing" in line for line in found), found


def test_a_renamed_heading_names_its_anchor() -> None:
    page = _plant_page("### Remove the zone", "### Remove a zone")
    found = anchor_problems(page, installer_texts())
    assert len(found) == 1 and "#remove-the-zone" in found[0], found


def test_the_anchors_are_those_the_installer_prints() -> None:
    """The set is read from the installer's files, not typed here: it holds each form a
    link takes there, and every heading the messages send a reader to."""
    texts = {
        "a.sh": 'cv_out "${CV_PAGE}#from-the-variable and $CV_PAGE#not-braced"',
        "b.sh": 'page="$CV_PAGE"; cv_out "  ${page}#from-the-local"',
        "c.sh": 'say "https://docs.controlvar.com/self-hosting/aws-install/#in-full"',
        "d.sh": 'say "https://docs.controlvar.com/self-hosting/cdk/#another-page"',
    }
    assert installer_anchors(texts) == {
        "from-the-variable",
        "not-braced",
        "from-the-local",
        "in-full",
    }
    real = installer_anchors(installer_texts())
    assert {
        "what-has-been-tested",
        "confirm-the-alarm-email",
        "remove-the-zone",
        "if-you-do-not-have-a-zone-yet",
        "if-uninstall-stops-at-the-network",
    } <= real, real


def test_a_link_to_a_heading_the_page_lacks_is_named() -> None:
    texts = installer_texts()
    texts["lib/new.sh"] = 'cv_out "${CV_PAGE}#a-heading-the-page-does-not-have"'
    found = anchor_problems(_read(PAGE), texts)
    assert len(found) == 1 and "#a-heading-the-page-does-not-have" in found[0], found


def test_an_installer_with_no_links_does_not_pass_for_having_none() -> None:
    found = anchor_problems(_read(PAGE), {"a.sh": "true  # nothing to see"})
    assert any("found nothing" in line for line in found), found


@pytest.mark.parametrize(
    ("plant", "says"),
    [
        ("file", "elasticache-backups"),
        ("group", "experimentation-cache-staging-redis"),
        ("window", "02:00-03:00"),
        ("command", BACKUP_COMMAND),
    ],
)
def test_a_backup_name_that_drifts_from_the_files_is_named(
    plant: str, says: str
) -> None:
    page, sweep = _read(PAGE), _read(SWEEP_SH)
    stack, names = _read(REDIS_STACK), _read(NAMES_SH)
    if plant == "file":
        page = _plant_page(
            "us-west-2/elasticache-snapshots.txt", "us-west-2/elasticache-backups.txt"
        )
    elif plant == "group":
        page = _plant_page(
            "automatic.experimentation-redis-staging-redis-001-",
            "automatic.experimentation-cache-staging-redis-001-",
        )
    elif plant == "window":
        stack = _plant_text(
            REDIS_STACK,
            'snapshot_window="02:00-03:00"',
            'snapshot_window="04:00-05:00"',
        )
    else:
        page = _plant_page(
            f"{BACKUP_COMMAND} --profile",
            "aws elasticache describe-cache-clusters --profile",
        )
    found = backup_problems(page, sweep, stack, names)
    assert found and any(says in line for line in found), (plant, found)


def test_a_page_with_no_backup_line_does_not_pass_for_having_none() -> None:
    found = backup_problems("# Install on AWS with one command (beta)\n", "", "", "")
    assert any("no line of the cache's automatic backup" in line for line in found), (
        found
    )


@pytest.mark.parametrize("literal", [KEPT_LITERAL, KEEPS_LITERAL])
def test_a_kept_line_that_changes_on_either_side_is_named(literal: str) -> None:
    page, uninstall = _read(PAGE), _read(UNINSTALL_SH)
    assert kept_problems(page.replace(literal, "Left: x"), uninstall) == [
        f"{PAGE}: the screens no longer show {literal!r}"
    ]
    assert kept_problems(page, uninstall.replace(literal, "Left: x")) == [
        f"{UNINSTALL_SH}: no longer prints {literal!r}"
    ]
