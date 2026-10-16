"""chart-kind's N-1 -> N delta reads this PR's revisions before N-1 is pulled.

``scripts/chart_kind.sh upgrade-previous`` asserts that the migrate containers
applied exactly the revisions this PR's image has and N-1's does not.  N-1 is
usually the release ``VERSION`` names, so its image tag, ``<profile>-<prev>``,
is the very tag ``load`` gave this PR's image (``<profile>-<appVersion>``).
``docker pull`` moves that tag to N-1's image.  When "this PR's revisions" were
read through the tag after the pull, they were N-1's own, the delta was empty
however many migrations the PR added, and the first PR to add one (#343) failed
with "the migrate containers applied [d12cbd384bbe], the delta is []".

The cluster job is the real gate; this pins the order it depends on, without a
cluster: this PR's revisions come from the image ID ``load`` recorded, read
before the first ``docker pull``, and nothing reads them through the tag.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

SCRIPT = pathlib.Path(__file__).resolve().parents[4] / "scripts" / "chart_kind.sh"


def _function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{name}\(\) {{\n(.*?)^}}", text)
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(1)


def test_this_prs_revisions_are_read_by_image_id_before_the_pull():
    body = _function("do_upgrade_previous")
    read_this = body.find('revisions "$this_api" >"$WORK/revisions-this"')
    # The pull is pull_anonymously (test_chart_kind_previous_public.py).
    first_pull = body.find("pull_anonymously ")
    assert read_this != -1, (
        "this PR's revisions are not read from the recorded image ID"
    )
    assert first_pull != -1, "the N-1 pull is gone: re-check what this test pins"
    assert read_this < first_pull, (
        "this PR's revisions are read after the N-1 pull, which can move the "
        "tag they would be read through onto N-1's image"
    )
    assert "sed -n 's/^api=//p' \"$WORK/images.txt\"" in body


def test_nothing_reads_this_prs_revisions_through_the_shared_tag():
    body = _function("do_upgrade_previous")
    assert '$PROFILE-$(app_version)" >"$WORK/revisions-this"' not in body
    assert body.count('>"$WORK/revisions-this"') == 1
    # N-1's revisions come from the job's private local tag, which nothing
    # else writes, under the api repository N-1's own chart names.
    assert 'revisions "$papi:$ltag" >"$WORK/revisions-previous"' in body
