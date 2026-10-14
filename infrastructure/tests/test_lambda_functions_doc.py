"""The docs say no Lambda function serves requests (#125).

`integrations/aws.md` and `getting-started/architecture.md` described working
assignment, event-processor and flag-evaluation Lambdas. The stacks deploy two
placeholders whose inline code returns 200 and the Glue ETL trigger; the code
under backend/lambda/ is deployed by nothing. ``test_lambda_functions_deployed.py``
synthesises prod (full profile) and holds that list; this file holds the pages
to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _pages() -> dict:
    return {
        k: " ".join(p.read_text(encoding="utf-8").split()) for k, p in PAGES.items()
    }


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
