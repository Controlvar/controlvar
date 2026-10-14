"""What the deploy documentation promises an operator (UX section B, CHECK 6).

`mkdocs build --strict` checks links between documents; it cannot see a link
printed by a workflow's `::error::`. These do: every
`docs/<path>.md#<anchor>` a refusal sends an operator to must be a heading
that exists. And the runbook must be usable for either environment, without
the step that deleted a published release tag mid-incident.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import jmespath
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DOCS = REPO_ROOT / "docs"
RUNBOOK = DOCS / "deployment" / "rollback-runbook.md"
GUIDE = DOCS / "deployment" / "deployment-guide.md"

pytestmark = pytest.mark.skipif(not DOCS.is_dir(), reason="this tree has no docs")


def _slug(heading: str) -> str:
    """The anchor mkdocs' default `toc` extension gives a heading."""
    text = re.sub(r"[`*]", "", heading.strip().lower())
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s]+", "-", text).strip("-")


def _anchors(path: Path) -> set[str]:
    return {
        _slug(m.group(1))
        for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", path.read_text(), re.M)
    }


@pytest.mark.regression
def test_every_doc_anchor_a_workflow_prints_exists():
    links = set()
    sources = [WORKFLOWS / n for n in ("deploy.yml", "rollback.yml", "db-migrate.yml")]
    # The scripts they run print links too (shift_traffic.py's wrong-route error).
    sources += sorted((REPO_ROOT / "scripts").glob("*.py"))
    sources += sorted((REPO_ROOT / "scripts").glob("*.sh"))
    for path in sources:
        text = path.read_text()
        links |= set(re.findall(r"(docs/[\w/.-]+\.md)#([\w-]+)", text))
    assert (
        "docs/deployment/deployment-guide.md",
        "the-api-route-and-the-primary-task-set-disagree",
    ) in links
    # The alarm copy's two runbook sections (#148, UX C5).
    assert (
        "docs/deployment/rollback-runbook.md",
        "an-alarm-rolled-the-api-back",
    ) in links
    assert (
        "docs/deployment/rollback-runbook.md",
        "fix-forward-while-an-alarm-is-firing",
    ) in links
    assert len(links) >= 2, f"found only {links}: the scan is not reading"
    for doc, anchor in sorted(links):
        path = REPO_ROOT / doc
        assert path.is_file(), f"a workflow links {doc}, which does not exist"
        assert anchor in _anchors(path), (
            f"a workflow sends operators to {doc}#{anchor}; that heading does "
            f"not exist (it has {sorted(_anchors(path))})"
        )


@pytest.mark.regression
def test_the_runbook_works_for_either_environment():
    text = RUNBOOK.read_text()
    fences = re.findall(r"```bash\n(.*?)```", text, re.S)
    code = "\n".join(fences)
    # One line sets the environment, and the sentence before it names the
    # other one: a trailing `# or staging` on that line is a comment zsh runs.
    assert re.search(r"^export ENV=prod$", code, re.M), "no line sets ENV"
    assert "`export ENV=staging`" in text, "the prose does not name staging"
    for literal in ("experimentation-prod", "backend-prod", "platform-prod", "-prod-"):
        assert literal not in code, f"a runbook command names {literal!r}"


@pytest.mark.regression
def test_the_runbook_shows_revisions_by_digest():
    """A deploy registers `backend@sha256:...`; a sample showing a version tag
    teaches the operator to look for something that is never there."""
    text = RUNBOOK.read_text()
    assert not re.search(r"backend:v\d", text), re.findall(r".*backend:v\d.*", text)
    assert "backend@sha256:" in text


#: `ENV=prod   # or staging`: bash reads a comment; zsh (interactive_comments
#: off, its default) reads a command named `#` with ENV=prod in ITS
#: environment, so the shell's ENV keeps whatever it was -- mid-rollback, the
#: other environment. The choice goes in the sentence before the block.
_COMMENTED_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?[A-Za-z_]\w*=\S*[ \t]+#", re.M
)


@pytest.mark.regression
@pytest.mark.parametrize(
    "path", sorted((DOCS / "deployment").glob("*.md")), ids=lambda p: p.name
)
def test_no_deploy_command_assigns_a_variable_beside_a_comment(path):
    fences = re.findall(
        r"^```(?:bash|sh|shell|console)\n(.*?)^```", path.read_text(), re.S | re.M
    )
    assert fences, f"{path.name}: no shell fences read; the scan is not reading"
    hits = [m.group(0) for body in fences for m in _COMMENTED_ASSIGNMENT.finditer(body)]
    assert not hits, f"{path.name}: {hits}"


@pytest.mark.regression
def test_the_runbook_does_not_delete_a_release_tag():
    text = RUNBOOK.read_text()
    assert ":refs/tags/" not in text
    assert "git tag -d" not in text


#: Where the deploy path is described to an operator.
CANARY_DOCS = [
    *sorted((DOCS / "deployment").glob("*.md")),
    DOCS / "self-hosting" / "cdk.md",
    DOCS / "integrations" / "aws.md",
]

#: Claims that the canary judges a release, or that CodeDeploy undoes a bad
#: one by itself. Written when no alarm was attached to the deployment group
#: (DECISIONS T21), and all seven stay after #148 (PE condition 10): the
#: alarms watch the API's target 5xx only, while a deployment is active, so a
#: release that answers wrongly with a 2xx still reaches 100%, the dashboard is
#: never rolled back, and nothing acts after the hour. The docs say "rolls the
#: API back by itself", with those limits beside it.
FALSE_CANARY_CLAIMS = [
    r"canary\s+is\s+the\s+gate",
    r"canary\s+(?:acts\s+as|is)\s+(?:a|the)\s+(?:gate|safety\s+net|check)",
    r"automatically\s+(?:shifts?|rolls?)\s+(?:traffic\s+)?back",
    r"shifts?\s+back\s+to\s+the\s+blue[^.\n]{0,40}automatically",
    r"configured\s+to\s+automatically\s+roll\s+back",
    r"no\s+manual\s+action\s+is\s+required",
    r"canary\b[^.\n]{0,40}\bwith\s+(?:CodeDeploy's\s+)?automatic\s+rollback",
]


@pytest.mark.regression
@pytest.mark.parametrize("path", CANARY_DOCS, ids=lambda p: p.name)
def test_no_document_says_the_canary_judges_a_release(path):
    """EM C5: the canary judges nothing; since #148 the alarms act on target
    5xx only, in the deployment's window, and rollback.yml is the response
    to everything else."""
    text = " ".join(path.read_text().split())
    hits = [
        m.group(0)
        for pattern in FALSE_CANARY_CLAIMS
        for m in re.finditer(pattern, text, re.I)
    ]
    assert not hits, f"{path.relative_to(REPO_ROOT)} claims: {hits}"


def test_all_seven_false_canary_claims_are_kept():
    """PE condition 10: #148 inverts the timed-only wording and drops no ban."""
    assert len(FALSE_CANARY_CLAIMS) == 7
    joined = " | ".join(FALSE_CANARY_CLAIMS)
    for kept in (
        "no\\s+manual\\s+action",
        "canary\\s+is\\s+the\\s+gate",
        "automatically",
    ):
        assert kept in joined, kept


@pytest.mark.regression
@pytest.mark.parametrize(
    "path", [DOCS / "deployment" / "README.md", GUIDE, RUNBOOK], ids=lambda p: p.name
)
def test_the_deploy_docs_say_what_the_alarms_watch(path):
    """Inverted, not deleted (#148 PE condition 10), from
    test_the_deploy_docs_say_the_canary_is_timed_only (T21, EM C5/C6).

    That test required "the canary is timed only" and, for the guide and the
    runbook, that alarm-based rollback (#148) was the founder-waivable
    prerequisite for production. With the alarms in place each page must say
    what they watch and, as plainly, what they do not: the 2xx case, the
    dashboard, and the load balancer's own 502/504. The waiver sentence goes.
    The "not yet run against a real AWS account" caveat went in #798: the
    forward deploy has run on staging, and the pages say what has run there.
    """
    text = " ".join(path.read_text().split())
    assert not re.search(r"canary is timed only", text, re.I), path.name
    assert re.search(r"Alarms watch the API", text), path.name
    assert "experimentation-api-5xx-blue-" in text, path.name
    assert "experimentation-api-5xx-green-" in text, path.name
    assert "2xx" in text, path.name
    assert "502" in text and "504" in text, path.name
    assert "dashboard" in text, path.name
    assert "issues/148" in text, path.name
    assert "rollback-runbook.md#an-alarm-rolled-the-api-back" in text or (
        path == RUNBOOK and "(#an-alarm-rolled-the-api-back)" in text
    ), path.name
    # EM condition 14: the waiver sentence goes only with the caveat kept.
    assert "waived it in writing" not in text, path.name
    assert "unless the founder waives it" not in text, path.name
    if path != RUNBOOK:
        assert "Not yet run against a real AWS account" not in text, path.name
        assert "What has run against a real AWS account" in text, path.name


def _runbook_section(heading: str) -> str:
    text = RUNBOOK.read_text()
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end >= 0 else len(text)]


@pytest.mark.regression
def test_the_break_glass_is_explained_in_one_place():
    """EM condition 4(f)/(g): only the runbook's "Fix forward while an alarm is
    firing" names the input, and it says when to use it, what it gives up, that
    the next deploy is watched again, and that the CloudWatch tricks do not
    unblock a deploy (unverified until the first staging rehearsal)."""
    section = _runbook_section("Fix forward while an alarm is firing")
    flat = " ".join(section.split())
    assert "`override_alarms`" in flat and "`override_alarms_reason`" in flat
    assert "the bug is in the release you would roll back to as well" in flat
    assert "a dependency outage holds the alarm in ALARM" in flat
    assert "no alarm watches that deployment" in flat
    assert "nothing rolls it back by itself" in flat
    assert "the next deploy is watched again with no action from anyone" in flat
    assert "`aws cloudwatch disable-alarm-actions`" in flat
    assert "`aws cloudwatch set-alarm-state`" in flat
    assert "do not unblock a deploy" in flat
    assert "not yet verified" in flat
    assert "ALARMS OVERRIDDEN" in flat
    # Nowhere else under docs/ names the input.
    others = [
        str(p.relative_to(REPO_ROOT))
        for p in sorted(DOCS.rglob("*.md"))
        if "override_alarms" in p.read_text().replace(section, "")
    ]
    assert not others, others


@pytest.mark.regression
def test_the_alarm_section_says_what_to_do_and_not_do():
    """UX's runbook section, in its order: confirm, history, the API, the
    dashboard, no Rollback while CodeDeploy's is active, the migration, no
    redeploy of the tag, and the false alarm."""
    flat = " ".join(_runbook_section("An alarm rolled the API back").split())
    order = [
        "Confirm it.",
        "See what the alarm saw.",
        "Check the API is back",
        "The dashboard.",
        "Do not dispatch Rollback while CodeDeploy's rollback is active.",
        "The migration stays applied.",
        "Do not redeploy the same tag.",
        "A false alarm",
    ]
    positions = [flat.index(step) for step in order]
    assert positions == sorted(positions), order
    assert "Method 2's dashboard block" in flat
    assert "`codeDeployRollback`" in flat


#: C9, as a test (PE condition 10): the old wording, anywhere an operator reads.
_TIMED_ONLY = re.compile(r"timed only|nothing rolls back", re.I)


@pytest.mark.regression
def test_no_timed_only_wording_is_left():
    """`git grep -i -E 'timed only|nothing rolls back' -- docs .github scripts`
    is empty -- walked in Python, so it also holds in a tree with no .git."""
    roots = [DOCS, REPO_ROOT / ".github", REPO_ROOT / "scripts"]
    files = [
        p
        for root in roots
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".md", ".yml", ".yaml", ".py", ".sh", ".txt"}
    ]
    assert any(p.name == "deploy.yml" for p in files), "the walk is not reading"
    assert GUIDE in files and RUNBOOK in files
    hits = [
        f"{p.relative_to(REPO_ROOT)}:{n}: {line.strip()[:120]}"
        for p in files
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
        if _TIMED_ONLY.search(line)
    ]
    assert not hits, hits


@pytest.mark.regression
@pytest.mark.parametrize(
    "path", [DOCS / "deployment" / "README.md", GUIDE], ids=lambda p: p.name
)
def test_the_docs_say_what_has_run_against_aws(path):
    """Inverted from test_the_docs_say_the_deploy_path_is_unproven (EM B3b C8)
    by #798. The forward deploy has run to success on staging; the Rollback
    workflow has run there but not yet completed end to end. Each page says
    both, and neither still calls the whole path unrun. The note is a
    blockquote, so its `>` markers are dropped before the text is joined."""
    text = " ".join(re.sub(r"(?m)^>[ \t]?", "", path.read_text()).split())
    assert "Not yet run against a real AWS account" not in text, path.name
    assert "forward deploy" in text and "has run to success on staging" in text
    assert (
        "The Rollback workflow has run on staging but has not yet completed "
        "end to end there" in text
    ), path.name


@pytest.mark.regression
def test_the_public_docs_do_not_name_internal_streams():
    """Operators read "the first staging deploy", not a plan's stream name."""
    hits = [
        f"{p.relative_to(REPO_ROOT)}:{n}"
        for p in sorted(DOCS.rglob("*.md"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"\bStream [A-Z]\b", line)
    ]
    assert not hits, hits


@pytest.mark.regression
@pytest.mark.parametrize("path", [GUIDE, RUNBOOK], ids=lambda p: p.name)
def test_the_fix_forward_cost_is_written_down(path):
    """EM B3b C5: within the hour, Rollback first; then its own hour."""
    text = " ".join(path.read_text().split())
    assert re.search(r"Rollback first", text), path.name
    assert "the next forward deploy is refused until it is no longer active" in text
    assert "expected behaviour" in text and "first staging deploy" in text


@pytest.mark.regression
def test_the_policy_upgrade_step_is_written_down():
    """PE B3b C6: roles made before the four new read actions need the policy."""
    text = " ".join((DOCS / "deployment" / "iam-permissions.md").read_text().split())
    assert "must have it re-applied" in text
    for action in (
        "cloudformation:DescribeStackResources",
        "elasticloadbalancing:DescribeListeners",
        "elasticloadbalancing:DescribeRules",
        "elasticloadbalancing:DescribeTargetHealth",
    ):
        assert action in text, action
    assert "Could not tell whether the API is serving" in text
    assert "Nothing shifts" in text
    # #148 PE condition 12: the alarm override's permission, why it is not
    # narrower, and the re-apply before the first cdk deploy with the alarms.
    assert "`codedeploy:UpdateDeploymentGroup`" in text
    # #777: the docs name the flag and describe its effect; they quote no
    # value, so a value copied out of them cannot be the shorthand CodeDeploy
    # refused ("Alarm list cannot be null").
    assert "with the group's alarms overridden (`--override-alarm-configuration`)" in (
        text
    )
    runbook = " ".join(
        (DOCS / "deployment" / "rollback-runbook.md").read_text().split()
    )
    assert "overridden (`--override-alarm-configuration`), always" in runbook
    for doc, body in (("iam-permissions.md", text), ("rollback-runbook.md", runbook)):
        assert not re.search(r"--override-alarm-configuration[ =]+[^`\s]", body), doc
    assert "there is no narrower action" in text
    assert "`iam:PassRole` is granted only to ECS tasks" in text
    assert (
        "Roles created from a policy before the alarms lack "
        "`codedeploy:UpdateDeploymentGroup`; re-apply this policy in every "
        "account before the first `cdk deploy` that adds the alarms." in text
    )
    assert "`AccessDenied` in the middle of an incident" in text
    # #148 PR-3 (#297), EM condition 11: the pre-flight's two reads, and the
    # re-apply before the first deploy that runs it.
    assert "`codedeploy:GetDeploymentGroup`" in text
    assert "`cloudwatch:DescribeAlarms`" in text
    assert (
        "Roles created from a policy before the pre-flight lack "
        "`codedeploy:GetDeploymentGroup` and `cloudwatch:DescribeAlarms`; "
        "re-apply this policy in every account before the first deploy of a "
        "release that includes the pre-flight." in text
    )
    assert "Could not read the API's alarms" in text


def test_the_guide_has_the_ordered_checklist():
    headings = [
        m.group(1) for m in re.finditer(r"^### (1\.\d) ", GUIDE.read_text(), re.M)
    ]
    assert headings == ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6"], headings
    text = GUIDE.read_text()
    # Protection before any variable or secret (EM C12).
    assert text.index("required reviewer") < text.index("Secrets `AWS_ACCOUNT_ID`")
    # The account ID is a secret (Stream I PE C1): the guide sets it as one.
    assert "gh secret set AWS_ACCOUNT_ID --env <env>" in text
    assert "gh variable set AWS_ACCOUNT_ID" not in text
    # The OIDC subject is decoded before the trust policy is written (PE C5).
    assert "Decode a\n  real token" in text or "decode a real token" in text.lower()


# --- the dashboard's deploy and rollback (#69) ---------------------------------


@pytest.mark.regression
def test_the_iam_doc_says_why_update_service_is_on_star_and_to_re_apply():
    """UpdateService on `*`, and why; and the re-apply before the first deploy."""
    text = " ".join((DOCS / "deployment" / "iam-permissions.md").read_text().split())
    assert "`ecs:UpdateService`" in text
    assert "on `*`" in text and "dominates" in text
    assert (
        "Roles created from a policy before the dashboard rollout lack "
        "`ecs:UpdateService`" in text
    )


@pytest.mark.regression
def test_the_guide_pins_the_dashboard_digest_and_the_runbook_covers_the_dashboard():
    assert "dashboard_image_tag=sha256:" in GUIDE.read_text()
    runbook = RUNBOOK.read_text()
    assert "deployments[?status=='PRIMARY']" in runbook
    assert "dashboard_task_definition_arn" in runbook
    assert "### The dashboard is the opposite case" in runbook


_RERUN = re.compile(r"re-?run(ning)? (the )?rollback|rollback again", re.I)


@pytest.mark.regression
def test_no_copy_advises_running_rollback_again():
    """EM ruling C3: a second Rollback inside the hour stops the first one's
    deployment with auto-rollback and puts the bad API release back. The only
    mention allowed is the warning against it.

    The scan covers every script (their ::error:: copy is operator copy too:
    shift_traffic.py, refuse_active_deployment.py) and every page under
    docs/deployment/, not only the three files the ban was first written for
    (#148 PE condition 8)."""
    texts = {
        "rollback.yml": (WORKFLOWS / "rollback.yml").read_text(),
        "deploy.yml": (WORKFLOWS / "deploy.yml").read_text(),
        "runbook": RUNBOOK.read_text(),
    }
    scripts = sorted((REPO_ROOT / "scripts").glob("*.py"))
    scripts += sorted((REPO_ROOT / "scripts").glob("*.sh"))
    pages = sorted((DOCS / "deployment").rglob("*.md"))
    # A glob that matches nothing makes this scan vacuous; say so instead.
    assert any(p.name == "shift_traffic.py" for p in scripts), scripts
    assert any(p.name == "refuse_active_deployment.py" for p in scripts), scripts
    assert RUNBOOK in pages and GUIDE in pages, pages
    for path in scripts + pages:
        texts.setdefault(str(path.relative_to(REPO_ROOT)), path.read_text())
    for where, text in texts.items():
        flat = " ".join(text.split())
        for match in _RERUN.finditer(flat):
            # The one allowed form is the warning: "Do not dispatch Rollback again".
            before = flat[max(0, match.start() - 20) : match.start()]
            assert before.endswith("Do not dispatch "), (
                where,
                flat[max(0, match.start() - 60) : match.end() + 40],
            )
    assert "Do not dispatch Rollback again while" in " ".join(texts["runbook"].split())


#: The verdicts a Rollback run ends with (scripts/rollback_verdict.py's rows,
#: #759); test_rollback_verdict.py pins that the two lists are equal.
VERDICT_WORDS = (
    "ROLLED BACK",
    "API BACK, DASHBOARD NOT",
    "API BACK, RUN FAILED",
    "NOT FINISHED",
    "STILL MOVING",
    "NOT ROLLED BACK",
    "OUTCOME UNKNOWN",
    "REFUSED",
    "NOTHING CHANGED",
)


@pytest.mark.regression
def test_the_runbook_reads_every_rollback_verdict():
    """#759: "Reading the result" has one row per verdict, and the STILL MOVING
    row, which the split-listener headline points at by this section's
    anchor, says a listener that stays split needs a person."""
    text = RUNBOOK.read_text()
    start = text.index("### Reading the result\n")
    section = text[start : text.index("\n### ", start + 1)]
    rows = re.findall(r"^\| `([A-Z ,]+)` \| .* \| (.*) \|$", section, re.M)
    assert [word for word, _ in rows] == list(VERDICT_WORDS)
    (moving,) = [action for word, action in rows if word == "STILL MOVING"]
    assert "the listener is stuck and needs a person" in moving
    assert "the table at the end of this page" in moving


# --- the Database Migration target (#726) -------------------------------------

DB_MIGRATE = WORKFLOWS / "db-migrate.yml"
#: Where an operator reads what to type into the Database Migration target.
TARGET_DOCS = [
    RUNBOOK,
    GUIDE,
    DOCS / "deployment" / "README.md",
]
#: `-1` as a token: not `-v-1H`, not `modules@-1`, not `2026-10-1`.
_MINUS_ONE = re.compile(r"(?<![\w@-])-1\b")
#: A sentence that mentions `-1` only to refuse it or warn against it.
_REFUSING = re.compile(r"(?i)\b(refus\w*|not|never)\b")


#: Clause boundaries: a sentence end, a dash aside, a list item, a paragraph.
#: A refusal in the next clause does not excuse this one ("a revision id or
#: `-1` to downgrade -- never the singular `head`").
_CLAUSE = re.compile(r"(?<=[.!?;])\s+|\s--\s|\n\s*(?:[-*]|\d+\.)\s|\n\s*\n")


def _sentences_recommending_minus_one(text: str) -> list[str]:
    sentences = _CLAUSE.split(text)
    return [
        " ".join(s.split())
        for s in sentences
        if _MINUS_ONE.search(s) and not _REFUSING.search(s)
    ]


def _db_migrate_target_description() -> str:
    document = yaml.safe_load(DB_MIGRATE.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document["on"]["workflow_dispatch"]["inputs"]["target"]["description"]


@pytest.mark.regression
def test_the_db_migrate_help_names_an_id_and_does_not_suggest_minus_one():
    description = _db_migrate_target_description()
    assert "37dcb2969766" in description
    # PE v2 C8: the modules example is not there on a core image.
    assert "modules_0001_rbac exists only in a full-profile image" in description
    assert _sentences_recommending_minus_one(description) == []
    # The whole workflow, comments and error text included (:112, :114): the
    # two phrasings that offered a relative step as a valid target.
    text = DB_MIGRATE.read_text(encoding="utf-8")
    assert not re.search(r"(?i)\bor a relative step\b", text)
    assert not re.search(r"(?i)relative step such as -1", text)


@pytest.mark.regression
@pytest.mark.parametrize("path", TARGET_DOCS, ids=lambda p: p.name)
def test_no_deploy_doc_tells_an_operator_to_downgrade_by_minus_one(path):
    assert _sentences_recommending_minus_one(path.read_text(encoding="utf-8")) == []


@pytest.mark.regression
def test_the_runbook_downgrade_names_the_down_revision():
    text = RUNBOOK.read_text(encoding="utf-8")
    (target,) = re.findall(r"^\s*- \*\*Target:\*\*.*$", text, re.M)
    assert "down_revision" in target
    assert "a7b8c9d0e1f2" in target, "the branch-point warning is gone"


@pytest.mark.regression
def test_the_migration_docs_do_not_say_the_workflow_suggests_minus_one():
    self_hosting = (DOCS / "self-hosting" / "migrations.md").read_text(encoding="utf-8")
    assert "still suggests" not in self_hosting
    assert "The Database Migration\nworkflow refuses `-1`." in self_hosting
    models = (DOCS / "architecture" / "models.md").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*alembic downgrade -1\b", models)


# --- undoing a migration (#726) -------------------------------------------------


@pytest.mark.regression
def test_the_runbook_undoes_a_migration_before_the_api_rollback():
    """#726, PE v2 condition 6. db-migrate.yml takes its image from the API's
    PRIMARY task set and has no image input, so once the API is rolled back
    the serving image lacks the migration's file and `alembic` cannot locate
    the database's revision: the workflow snapshots, then fails, and migrates
    nothing. The runbook must put the downgrade first, say there is no
    supported downgrade after an API or alarm rollback, and put the STOP
    paragraph (a redeploy does not reach a restored cluster) ahead of the
    restore, which is now the decided path's link (T143), not a command."""
    section = _runbook_section("Database Rollback Procedure")
    flat = " ".join(section.split())
    # (c) the old order is gone, from the section and from the decision tree.
    assert "first or in parallel" not in flat
    assert not re.search(r"redeploy the previous application version", flat)
    assert "Method 1 + DB Rollback section" not in RUNBOOK.read_text()
    assert "DATABASE FIRST, THEN APPLICATION" in RUNBOOK.read_text()
    assert "Application rollback alone did not resolve the issue" not in flat
    # (a) the downgrade works only while the migrating release is PRIMARY.
    assert "Undo the migration before you roll the API back." in flat
    assert "works only while the release that contains the migration is PRIMARY" in flat
    # (b) no supported downgrade afterwards; the restore; the Engineering Lead.
    assert (
        "After an API rollback or an alarm rollback, there is no supported "
        "downgrade through the workflow." in flat
    )
    assert "Call the Engineering Lead now." in flat
    # Never "snapshot restore" while the command is a point-in-time restore.
    assert not re.search(
        r"snapshot restore|restore from (aurora )?snapshot", flat, re.I
    )
    assert not re.search(r"restore the snapshot", RUNBOOK.read_text(), re.I)
    # The STOP paragraph, then the decided path, then the restore by link: the
    # section prints no restore of its own (it had no instance and two
    # placeholders), and no "decide before an incident" any more.
    stop = flat.index(
        "**STOP. A restore to a NEW cluster is not picked up by a redeploy.**"
    )
    decided = flat.index(
        "The decided way to point the application at it is `scripts/restore_repoint.sh`"
    )
    link = flat.index(
        "with Steps 1 to 4 of [Restore from PITR](disaster-recovery.md#restore-from-pitr-preferred)"
    )
    assert stop < decided < link, (stop, decided, link)
    assert "That path is **decided, not yet rehearsed**" in flat
    assert "aws rds restore-db-cluster-to-point-in-time" not in section
    assert "Decide which BEFORE an incident" not in section
    assert "issue 78" not in section
    assert "restore-from-pitr-preferred" in _anchors(
        DOCS / "deployment" / "disaster-recovery.md"
    )
    # AWS restores to any second in the retention period, not a 5-minute window.
    assert "5-minute window" not in RUNBOOK.read_text()
    assert "RTO for a point-in-time restore: ~30 minutes" not in section

    # Its copy never added an instance and left two of the cluster's network
    # settings as placeholders (#190).
    assert "<the cluster's DB subnet group>" not in section
    assert "<aurora-sg-id>" not in section


def _restore_section() -> str:
    """The disaster-recovery page's "Restore from PITR" section."""
    page = (DOCS / "deployment" / "disaster-recovery.md").read_text(encoding="utf-8")
    start = page.index("### Restore from PITR (preferred)")
    return page[start : page.index("\n### ", start + 1)]


@pytest.mark.regression
def test_the_restore_reads_and_passes_the_original_clusters_settings():
    """A restore given no parameter group gets the engine's default one, with no
    error, so the settings the database stack makes are lost. The page's
    restore is now `scripts/restore_repoint.sh restore`, which reads every
    value from the original and the stack and passes it; that is pinned call by
    call in test_restore_repoint.py (test_the_restore_passes_every_setting_it_read).
    Here: the page says what it passes, refuses on a difference, and types no
    group name or instance class anywhere."""
    section = _restore_section()
    flat = " ".join(section.split())
    assert "scripts/restore_repoint.sh restore" in section
    for passed in (
        "the original's subnet group, VPC groups and cluster parameter group",
        "`--copy-tags-to-snapshot`",
        "the original writer's class, parameter group, promotion tier and tags",
        "`restore` then refuses unless both match the original",
    ):
        assert passed in flat, passed
    blocks = re.findall(r"```bash\n(.*?)```", section, re.S)
    assert blocks, "the section has no shell block: the scan reads nothing"
    for block in blocks:
        assert not re.search(r"parameter-group-name\s+[^\s$\"']", block), block
        assert not re.search(r"--db-instance-class\s+[^\s$\"']", block), block


# --- the decided restore path (T143) ---------------------------------------------

DR = DOCS / "deployment" / "disaster-recovery.md"
REPOINT = REPO_ROOT / "scripts" / "restore_repoint.sh"


def _dr_section(heading: str, level: str = "##") -> str:
    text = DR.read_text(encoding="utf-8")
    start = text.index(f"\n{level} {heading}\n")
    end = text.find(f"\n{level} ", start + 1)
    return text[start : end if end >= 0 else len(text)]


@pytest.mark.regression
def test_the_restore_path_is_the_script_and_says_what_it_cannot_do():
    """T143: the restore is scripts/restore_repoint.sh, labelled "decided, not
    yet rehearsed" until a staging rehearsal passes; it needs an `available`
    cluster, so DR Scenario 4's own trigger (`failed`) has no decided path;
    prod's reader step has run only against the fake; no repoint time is given
    as measured. Every phase the page runs is one the script has, each in a
    block of its own, so no pasted block runs `restore` and `cutover`
    together."""
    scenario = _dr_section("Scenario 4: Full Aurora Database Cluster Failure")
    flat = " ".join(scenario.split())
    assert "**Decided, not yet rehearsed.**" in flat
    assert "**A cluster in status `failed` has no decided path.**" in flat
    assert "Prod's `readers` step has run only against that fake" in flat
    assert "an estimated 15 to 40 minutes, not measured" in flat
    assert "plus the cutover's downtime, which is not measured yet" in flat
    assert "Decide before an incident" not in flat
    assert "#### If the script stops part-way" in scenario
    # The downtime's monitors are expected, not measured (EM ruling R3).
    assert "the load balancer is expected to answer 503 to every request" in flat
    assert "measured; the staging rehearsal records them. A green" in flat
    # The rehearsal's own step numbers are not published, so the page names none.
    assert "step R5" not in flat
    assert "the load balancer answers 503" not in flat
    # A probe timeout goes to rollback, never to start-api (R3).
    assert "or the probes did not pass within 30 minutes (`probe.log`)" in flat
    # keep, then rollback, leaves the stack's cluster protected (R1).
    assert (
        "the original comes back onto the stack's names with deletion protection on, "
        "which the database stack does not set"
    ) in flat
    # The phases the script dispatches: the case labels after `cmd=`.
    dispatch = REPOINT.read_text().split("cmd=${1:-}", 1)[1]
    labels = re.findall(r"^\s*([a-z-]+(?: \| [a-z-]+)*)\)", dispatch, re.M)
    known = {phase for label in labels for phase in label.split(" | ")}
    assert {
        "read",
        "restore",
        "cutover",
        "readers",
        "rollback",
        "keep",
        "start-api",
    } <= known
    blocks = re.findall(r"```bash\n(.*?)```", scenario, re.S)
    used = []
    for block in blocks:
        run = re.findall(r"scripts/restore_repoint\.sh ([a-z-]+)", block)
        assert len(run) <= 1, block
        used += run
    assert used == ["read", "restore", "cutover", "readers", "keep", "rollback"], used
    assert set(used) <= known
    # No restore by hand on the page any more: it had no parameter group.
    assert "aws rds restore-db-cluster-to-point-in-time" not in scenario


@pytest.mark.regression
def test_scenario_8_records_before_it_stops_and_types_no_count():
    """Scenario 8 typed `--desired-count 3` and `--min-capacity 3` (staging
    runs 2), and its restore had no subnet group, VPC group, parameter group or
    instance, with a placeholder that fails `bash -n`. It now records the API
    with `read` before it stops it, restores through the decided path, and
    the cutover puts the recorded count back."""
    scenario = _dr_section("Scenario 8: Complete Data Loss")
    assert "--desired-count 3" not in scenario
    assert "--min-capacity 3" not in scenario
    assert "<timestamp-before-loss>" not in scenario
    assert "restore-db-cluster-to-point-in-time" not in scenario
    record = scenario.index('EVID=$(scripts/restore_repoint.sh read "$ENV")')
    stop = scenario.index("--desired-count 0")
    assert record < stop, "read must record the API before it is stopped"
    assert "[Restore from PITR](#restore-from-pitr-preferred)" in scenario
    for block in re.findall(r"```bash\n(.*?)```", scenario, re.S):
        done = subprocess.run(
            ["bash", "-n"], input=block, capture_output=True, text=True
        )
        assert done.returncode == 0, (block, done.stderr)


def _key_rotation_section() -> str:
    """secrets-management.md's "Rotate Compromised API Key" section."""
    page = (DOCS / "deployment" / "secrets-management.md").read_text(encoding="utf-8")
    start = page.index("### Rotate Compromised API Key")
    end = page.find("\n### ", start + 1)
    return page[start : end if end != -1 else len(page)]


@pytest.mark.regression
def test_the_key_rotation_runbook_names_what_the_api_writes():
    """#251: the runbook searched a log group no stack creates, filtered on a
    field the API never writes, used a `date` form that fails on macOS, and
    made the replacement key with no scopes. Each claim is pinned to the code."""
    section = _key_rotation_section()
    stack = (
        REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "fargate_service_stack.py"
    ).read_text(encoding="utf-8")
    assert 'log_group_name=f"/ecs/experimentation-backend-{env_name}"' in stack
    assert "/ecs/experimentation-backend-$ENV" in section
    assert "/experimentation-platform/api" not in section
    assert "date -d" not in section
    assert '"scopes"' in section, "step 2 must recreate the key with its scopes"
    for page in DOCS.rglob("*.md"):
        assert "api_key_prefix" not in page.read_text(encoding="utf-8"), page
    api_keys = (DOCS / "security" / "api-keys.md").read_text(encoding="utf-8")
    assert "not updated when a key is used" not in api_keys


#: The recovery pages' AWS CLI commands the flag check below reads: page ->
#: the `service operation` commands it must find there (so that a scan that
#: reads nothing fails), and every command of those services is then checked.
#: Only the flags before a `$(` are read, so a command nested in an argument is
#: not checked against the outer one: a page that gains one needs a parser.
CLI_FLAG_CHECKED = {
    # Its point-in-time restore is scripts/restore_repoint.sh now, whose own
    # flags test_restore_repoint.py checks against the same models.
    "disaster-recovery.md": {
        "rds describe-db-clusters",
        "rds describe-db-cluster-snapshots",
        "rds restore-db-cluster-from-snapshot",
        "cloudformation describe-stacks",
    },
    "rollback-runbook.md": {"rds describe-db-cluster-snapshots"},
    "secrets-management.md": {
        "rds modify-db-cluster",
        "logs filter-log-events",
    },
}


@pytest.mark.regression
@pytest.mark.parametrize("page", sorted(CLI_FLAG_CHECKED))
def test_every_aws_flag_the_recovery_pages_print_is_one_the_cli_has(page):
    """`aws <service> <operation>` takes its flags from botocore's service
    model, which is also what the AWS CLI reads: a flag that is not a member of
    the operation's input is refused with exit 252 when it is pasted
    mid-incident. Checked offline, for every `aws rds`, `aws logs` and
    `aws cloudformation` command the page prints."""
    from botocore import xform_name
    from botocore.session import get_session

    text = (DOCS / "deployment" / page).read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    found = set()
    for service in ("rds", "logs", "cloudformation"):
        model = get_session().get_service_model(service)
        flags_of = {
            xform_name(op).replace("_", "-"): {
                xform_name(member).replace("_", "-")
                for member in model.operation_model(op).input_shape.members
            }
            for op in model.operation_names
            if model.operation_model(op).input_shape is not None
        }
        for block in blocks:
            # Join the continuation lines, then read each command of the service.
            flat = block.replace("\\\n", " ")
            for operation, rest in re.findall(
                rf"aws {service} ([a-z0-9-]+)([^\n]*)", flat
            ):
                if operation == "wait":
                    continue
                assert operation in flags_of, (page, service, operation)
                found.add(f"{service} {operation}")
                # A `$(date ...)` in an argument is another command's flags.
                rest = rest.split("$(")[0]
                for flag in re.findall(r"(?<![\w-])--([a-z0-9-]+)", rest):
                    if flag not in ("query", "output"):
                        assert flag in flags_of[operation], (page, operation, flag)
    assert CLI_FLAG_CHECKED[page] <= found, (page, CLI_FLAG_CHECKED[page] - found)


# --- the canary's length (#212, D47) -------------------------------------------

#: Every file that states how long the API's canary holds traffic at 10%.
CANARY_LENGTH_SOURCES = [
    DOCS / "deployment" / "README.md",
    GUIDE,
    RUNBOOK,
    DOCS / "self-hosting" / "cdk.md",
    DOCS / "integrations" / "aws.md",
    WORKFLOWS / "deploy.yml",
    WORKFLOWS / "rollback.yml",
    REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "fargate_service_stack.py",
    REPO_ROOT / "scripts" / "refuse_active_deployment.py",
]

#: The canary was five minutes until #212. Anchored on the canary, so the
#: rollback's own "under 5 minutes" target, the RPO figures and the README's
#: "five minutes is often too short" (the reason for fifteen) do not match.
STALE_CANARY_LENGTH = [
    r"canary_10_percent_5_minutes",
    r"ecscanary10percent5minutes",
    r"canary(?:'s)?\s+(?:5|five)[- ]minutes?",
    r"canary[^.]{0,160}?\b(?:waits?|for|another|further|in\s+the)\s+(?:5|five)\s+minutes",
]
CURRENT_CANARY_LENGTH = re.compile(
    r"\b(?:15|fifteen)\s+minutes|canary_10_percent_15_minutes", re.I
)


def _whole_text(path: Path) -> str:
    """The file as one line: a sentence that wraps, in prose or in a `#`
    comment block, is matched whole."""
    text = re.sub(r"\n[ \t]*(?:#:?|//)?[ \t]*", " ", path.read_text())
    return " ".join(text.split())


def _stale_canary_claims(text: str) -> list[str]:
    return [
        m.group(0)
        for pattern in STALE_CANARY_LENGTH
        for m in re.finditer(pattern, text, re.I)
    ]


@pytest.mark.regression
@pytest.mark.parametrize("path", CANARY_LENGTH_SOURCES, ids=lambda p: p.name)
def test_the_canary_is_described_as_fifteen_minutes(path):
    """#212 (D47): nothing an operator reads still gives the canary five
    minutes, and each source says fifteen."""
    text = _whole_text(path)
    hits = _stale_canary_claims(text)
    assert not hits, (
        f"{path.relative_to(REPO_ROOT)}: canary still described as 5 minutes: {hits}"
    )
    assert CURRENT_CANARY_LENGTH.search(text), (
        f"{path.relative_to(REPO_ROOT)} does not say the canary is 15 minutes"
    )


def test_the_canary_length_check_is_not_vacuous():
    """Each stale shape is caught, wrapped over a line break; the rollback's
    "under 5 minutes" target is not."""
    for stale in (
        "The deployment group's canary sends 10%\nof traffic, waits five minutes, then",
        "a canary would leave 90% of traffic on it for a further\n# five minutes",
        "the canary's five minutes",
        "NOT the group's CANARY_10_PERCENT_5_MINUTES",
    ):
        flat = " ".join(re.sub(r"\n[ \t]*(?:#:?|//)?[ \t]*", " ", stale).split())
        assert _stale_canary_claims(flat), stale
    assert not _stale_canary_claims(
        "a canary would leave 90% of traffic on it for another fifteen minutes, "
        'against a runbook target of "under 5 minutes from decision to rollback'
    )


def _deploy_job() -> dict:
    return yaml.safe_load((WORKFLOWS / "deploy.yml").read_text())["jobs"]["deploy"]


def _role_duration_seconds(job: dict) -> int:
    (seconds,) = [
        step["with"]["role-duration-seconds"]
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("aws-actions/configure-aws-credentials")
    ]
    return int(seconds)


@pytest.mark.regression
def test_the_docs_give_the_deploy_jobs_own_timeout_and_session():
    """PE C3: the docs' numbers are read from deploy.yml, not retyped."""
    job = _deploy_job()
    timeout = int(job["timeout-minutes"])
    session = _role_duration_seconds(job)
    deadline = int(
        yaml.safe_load((WORKFLOWS / "deploy.yml").read_text())["env"][
            "CODEDEPLOY_DEADLINE_SECONDS"
        ]
    )
    guide = " ".join(GUIDE.read_text().split())
    assert f"The deploy job's timeout is {timeout} minutes:" in guide
    assert f"`CODEDEPLOY_DEADLINE_SECONDS` ({deadline // 60} minutes" in guide
    iam = " ".join((DOCS / "deployment" / "iam-permissions.md").read_text().split())
    assert (
        f"`role-duration-seconds` is {session} in `deploy.yml` (a {timeout}-minute job)"
        in iam
    )


DEPLOYMENT_DOCS = DOCS / "deployment"


def _fenced_lines(markdown: str) -> list[tuple[int, str]]:
    """Every uncommented line inside a fenced block, with its line number.

    Prose may name a banned command to say not to use it; a fenced block is
    what an operator pastes.
    """
    found: list[tuple[int, str]] = []
    inside = False
    number = 0
    for line in markdown.splitlines():
        number += 1
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside and not line.lstrip().startswith("#"):
            found.append((number, line))
    return found


@pytest.mark.regression
def test_no_services_stable_and_no_forced_deployment_in_the_deployment_docs():
    """#789: test_dashboard_deploy_wiring.py's ban on the workflows and
    scripts/, extended to the commands docs/deployment/ tells an operator
    to paste. The secret rotation procedures restarted the API with
    `--force-new-deployment` and waited with `services-stable`, neither of
    which is how a CODE_DEPLOY-controlled service restarts."""
    pages = sorted(DEPLOYMENT_DOCS.glob("*.md"))
    assert len(pages) >= 5, pages
    scanned = 0
    for page in pages:
        for number, line in _fenced_lines(page.read_text()):
            scanned += 1
            where = f"{page.name}:{number}"
            assert "--force-new-deployment" not in line, where
            assert "wait services-stable" not in line, where
    assert scanned > 500, scanned


def _markdown_section(page: Path, heading: str) -> str:
    text = page.read_text()
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    rest = text[start + len(heading) :]
    end = re.search(rf"^{level} ", rest, re.M)
    return heading + (rest[: end.start()] if end else rest)


@pytest.mark.regression
def test_the_restart_on_the_serving_revision_approves_its_shift_all_at_once():
    """#789: the restart the runbook gives (after a restore, and the manual path
    for a secret rotation) created a CodeDeploy deployment and never approved
    it, so the group's 30-minute approval wait stopped it; and it inherited the
    group's canary."""
    section = _markdown_section(
        DEPLOYMENT_DOCS / "rollback-runbook.md",
        "## Restart the API on the revision it is serving",
    )
    code = "\n".join(line for _, line in _fenced_lines(section))
    assert "aws deploy create-deployment" in code
    assert "--deployment-config-name CodeDeployDefault.ECSAllAtOnce" in code
    assert "aws deploy continue-deployment" in code
    assert "--deployment-wait-type READY_WAIT" in code
    assert "aws ecs register-task-definition" in code
    assert "scripts/api_serving.py" in code
    assert code.index("create-deployment") < code.index("continue-deployment")
    assert code.index("continue-deployment") < code.index("api_serving.py")
    # Every page that sends an operator to restart the API sends them here.
    runbook = (DEPLOYMENT_DOCS / "rollback-runbook.md").read_text()
    assert runbook.count("aws deploy create-deployment") == 2, (
        "one create-deployment in Method 2 and one in the restart section"
    )
    for page in ("disaster-recovery.md", "secrets-management.md"):
        assert (
            "rollback-runbook.md#restart-the-api-on-the-revision-it-is-serving"
            in (DEPLOYMENT_DOCS / page).read_text()
        ), page


@pytest.mark.regression
def test_the_jwt_rotation_does_not_claim_to_take_effect_at_once():
    """#789: old tasks keep accepting old-key tokens until the shift."""
    section = _markdown_section(
        DEPLOYMENT_DOCS / "secrets-management.md",
        "### Rotate JWT Secret Immediately",
    )
    assert "invalidates all active sessions immediately" not in section
    assert "#restart-the-api-on-its-current-release" in section
    assert "#how-long-the-old-value-keeps-working" in section


# ---------------------------------------------------------------------------
# The API's running counts: the PRIMARY task set's own, never the service's
# (#816).
#
# For the hour after a blue/green shift the replaced task set keeps running
# beside the new one, so the service's runningCount counts both (staging read
# 4 of 2 after a rollback that had worked). The commands that tell an operator
# "Running == Desired" read the PRIMARY task set's runningCount,
# computedDesiredCount and pendingCount instead. Scoped to the three documents
# that carry those commands and to the API's service: disaster-recovery.md and
# the dashboard's rolling-controller blocks are deliberately not read.

COUNT_DOCS = [RUNBOOK, GUIDE, DOCS / "deployment" / "README.md"]
POST_SHIFT = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "rollback_verify"
    / "services-post-shift.json"
)


def _api_count_commands() -> list[tuple[str, str]]:
    """(document, command) for every describe-services of the API's service
    that reads a count (runningCount, desiredCount, pendingCount,
    computedDesiredCount): the old service-level form is caught here too, and
    then fails the evaluation below.

    A command is the fence's `\\`-continued lines joined back into one, as the
    shell reads them.
    """
    found = []
    for path in COUNT_DOCS:
        if not path.is_file():  # read at collection; the module skips later
            continue
        text = path.read_text()
        for body in re.findall(r"^```bash\n(.*?)^```", text, re.S | re.M):
            logical, current = [], ""
            for line in body.split("\n"):
                current += line + "\n"
                if not line.endswith("\\"):
                    logical.append(current)
                    current = ""
            for command in logical:
                if (
                    "describe-services" in command
                    and "backend-" in command
                    and "Count" in command
                ):
                    found.append((path.name, command))
    return found


def _run_through_the_shell(command: str, tmp_path: Path) -> list[str]:
    """The arguments `aws` receives when the operator pastes `command`.

    The real shell does the quoting -- including `watch`'s second round
    through `sh -c` -- so a quote that ends a quoted string early changes
    what the fake receives exactly as it changes what the real CLI does.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    args_file = tmp_path / "aws-args"
    args_file.write_bytes(b"")
    (bin_dir / "aws").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['AWS_ARGS'], 'w') as f:\n"
        "    json.dump(sys.argv[1:], f)\n"
    )
    # watch(1) joins its arguments with spaces and runs them with `sh -c`.
    (bin_dir / "watch").write_text('#!/bin/sh\nshift 2\nexec sh -c "$*"\n')
    for name in ("aws", "watch"):
        (bin_dir / name).chmod(0o755)
    script = tmp_path / "block.sh"
    script.write_text("export ENV=staging\n" + command)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "AWS_ARGS": str(args_file),
        "HOME": str(tmp_path),
    }
    done = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    raw = args_file.read_text()
    assert raw, f"the block never ran aws: {command}"
    return json.loads(raw)


def test_the_api_count_commands_are_found():
    found = _api_count_commands()
    by_doc = {}
    for name, _ in found:
        by_doc[name] = by_doc.get(name, 0) + 1
    # Two in the runbook (the watch and the post-rollback check), one in the
    # guide, two in the README (the check and the watch).
    assert by_doc == {
        "rollback-runbook.md": 2,
        "deployment-guide.md": 1,
        "README.md": 2,
    }, by_doc


@pytest.mark.regression
@pytest.mark.parametrize(
    "doc,command",
    [
        pytest.param(
            doc, command, id=f"{doc}-{'watch' if 'watch' in command else 'check'}"
        )
        for doc, command in _api_count_commands()
    ],
)
def test_the_api_counts_are_the_primary_task_sets(doc, command, tmp_path):
    args = _run_through_the_shell(command, tmp_path)
    assert args[:2] == ["ecs", "describe-services"], args
    assert args[args.index("--services") + 1] == "experimentation-backend-staging"
    # --output text sorts the keys and an operator's config may set text or
    # table: the block says json itself.
    assert args[args.index("--output") + 1] == "json", args
    query = args[args.index("--query") + 1]
    response = json.loads(POST_SHIFT.read_text())
    (primary,) = [
        s for s in response["services"][0]["taskSets"] if s["status"] == "PRIMARY"
    ]
    got = jmespath.search(query, response)
    # The service runs 4 of 2 here; the PRIMARY task set runs 2 of 2.
    assert got == {
        "Serving": primary["taskDefinition"],
        "Running": 2,
        "Desired": 2,
        "Pending": 0,
    }, f"{doc}: {query!r} gives {got!r} on the post-shift response"


@pytest.mark.regression
def test_a_watch_block_writes_primary_in_backticks():
    """Inside `watch`'s single-quoted string, `'PRIMARY'` ends the quote: the
    filter compares against a field named PRIMARY, and the command prints
    null and exits 0."""
    watches = [c for _, c in _api_count_commands() if c.lstrip().startswith("watch")]
    assert len(watches) == 2, watches
    for command in watches:
        assert "status==\\`PRIMARY\\`" in command, command
        assert "'PRIMARY'" not in command, command
