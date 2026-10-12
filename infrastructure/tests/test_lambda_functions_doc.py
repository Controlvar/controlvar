"""The docs say which Lambda functions the CDK deploys, and that none serves requests (#125).

`integrations/aws.md` and `getting-started/architecture.md` described working
assignment, event-processor and flag-evaluation Lambdas. The stacks deploy two
placeholders whose inline code returns 200 and the Glue ETL trigger; the code
under backend/lambda/ is deployed by nothing. This synthesises prod (full
profile) and checks that list, and that the pages still say so. Deploying a
real function fails here until the pages describe it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_dashboard_service import _synth

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
PAGES = {
    "aws": DOCS / "integrations" / "aws.md",
    "architecture": DOCS / "getting-started" / "architecture.md",
    "faq": DOCS / "getting-started" / "faq.md",
    "technical-guide": DOCS / "architecture" / "technical-guide.md",
    "overview": DOCS / "architecture" / "overview.md",
}

pytestmark = pytest.mark.regression

#: The functions the stacks define, by logical-id prefix, and whether each is a
#: placeholder (inline code that only returns 200).
EXPECTED = {
    "DatabaseAccessLambda": True,
    "AnalyticsLambda": True,
    "ETLTriggerLambda": False,
}
#: CDK's own singleton providers (bucket auto-delete, log retention), which
#: are not application functions.
CDK_PROVIDER = re.compile(r"^(AWS[0-9a-f]{32}|CustomS3AutoDeleteObjects|LogRetention)")


@pytest.fixture(scope="module")
def functions() -> dict:
    """{logical-id prefix: properties} for every application Lambda in prod."""
    found = {}
    for stack in _synth("prod").stacks:
        for lid, r in stack.template.get("Resources", {}).items():
            if r["Type"] != "AWS::Lambda::Function" or CDK_PROVIDER.match(lid):
                continue
            prefix = re.sub(r"[0-9A-F]{8}$", "", lid)
            assert prefix not in found, f"two functions named {prefix}"
            found[prefix] = r["Properties"]
    return found


def _pages() -> dict:
    return {
        k: " ".join(p.read_text(encoding="utf-8").split()) for k, p in PAGES.items()
    }


def test_exactly_the_functions_the_docs_list(functions):
    assert set(functions) == set(EXPECTED), sorted(functions)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_placeholders_are_placeholders(functions, name):
    code = functions[name]["Code"]
    inline = code.get("ZipFile", "")
    if EXPECTED[name]:
        assert "'statusCode': 200" in inline, (name, inline)
        # Nothing but a return: no import, no client, no loop.
        assert "import" not in inline and "boto3" not in inline, (name, inline)
    else:
        assert "glue.start_job_run" in inline, (name, inline[:200])


def test_nothing_deploys_backend_lambda(functions):
    for name, props in functions.items():
        assert "S3Key" not in props["Code"], f"{name} is deployed from an asset"


def test_the_pages_say_no_lambda_serves_requests():
    pages = _pages()
    assert "No Lambda function serves requests or processes events" in pages["aws"]
    assert (
        "No Lambda function serves requests or processes events"
        in pages["architecture"]
    )
    assert "placeholder functions that do nothing" in pages["faq"]
    for key, text in pages.items():
        for gone in (
            "### Experiment Assignment Lambda",
            "### Event Processor Lambda",
            "### Feature Flag Evaluation Lambda",
            "Assignment Lambda —",
            "Event Processor Lambda →",
        ):
            assert gone not in text, (key, gone)


#: Claims that present code under backend/lambda/ as triggered, serving or
#: real-time, or describe the assignment Lambda, which was deleted (#480):
#: assignment is the API's (`POST /api/v1/tracking/assign`). Each phrase is
#: checked against every page, so moving a claim to another page in PAGES
#: still fails. `Experiment Assignment` alone is not listed: it is a fair
#: heading on five SDK pages.
GONE_EVERYWHERE = (
    "Assignment Lambda",
    "Assignment Service (Lambda)",
    "backend/lambda/assignment",
    "Real-time assignment",
    "Three AWS Lambda functions",
    "Lambda-based evaluation service",
    "Real-time Services (Lambda)",
    "Triggered by: Kinesis Data Stream",
    "Triggered by: API Gateway",
)
#: Phrases too common to ban everywhere, checked on one page. The overview's
#: bullet list under its old Lambda heading, in `_pages()`' normalised form:
#: removing the heading alone does not satisfy this while the bullet survives.
GONE_ON_PAGE = (("overview", "Feature Flag Evaluation - Experiment Assignment"),)


@pytest.mark.parametrize(
    ("page", "phrase"),
    [(page, phrase) for page in sorted(PAGES) for phrase in GONE_EVERYWHERE]
    + list(GONE_ON_PAGE),
)
def test_no_page_presents_an_undeployed_lambda_as_live(page, phrase):
    assert phrase not in _pages()[page], (page, phrase)
