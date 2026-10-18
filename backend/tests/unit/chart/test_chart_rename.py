"""Renaming the chart renames no object it renders.

Every object name and the ``app.kubernetes.io/name`` selector label come from
the ``controlvar.name`` and ``controlvar.fullname`` helpers. They used to
read ``.Chart.Name``, so a chart published under another name made
``helm upgrade`` render new objects beside the old ones: a second Deployment
whose selector matches nothing old, and a postgres StatefulSet with a new,
empty volume claim while the old one is orphaned. Both helpers now write the
stem.

These tests render the chart as it is and a copy whose ``Chart.yaml`` has a
different ``name:``, for two releases and both CI values files, and require
the same objects (kind and name), the same selectors and the same volume
claim names. Only the ``helm.sh/chart`` label, which names the chart, may
differ.

Like ``test_chart_render.py`` they run only in the ``chart`` job
(``EXPERIMENTLY_CHART_TESTS=1``, its pinned helm), which fails on a skip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
CHART = REPO_ROOT / "charts" / "experimently"
CI_VALUES = ("values-core.yaml", "values-full.yaml")
#: The name the copy is published under: any name but the chart's own.
OTHER_NAME = "example-chart"
RELEASES = ("experimently", "foo")

HELM = os.environ.get("HELM") or shutil.which("helm")
ENABLED = os.environ.get("EXPERIMENTLY_CHART_TESTS") == "1"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.regression,
    pytest.mark.skipif(
        not ENABLED,
        reason="EXPERIMENTLY_CHART_TESTS is not 1; these run in the chart job "
        "against its pinned helm",
    ),
    pytest.mark.skipif(HELM is None, reason="no helm binary ($HELM or PATH)"),
    pytest.mark.skipif(
        not CHART.is_dir(), reason="this tree has no charts/experimently"
    ),
]

Shape = Tuple[Set[Tuple[str, str]], Dict[str, Any]]


def renamed_copy(tmp: Path, name: str = OTHER_NAME) -> Path:
    """The chart, copied, with ``name:`` in Chart.yaml set to *name*."""
    copy = tmp / "experimently"
    shutil.copytree(CHART, copy)
    chart_yaml = copy / "Chart.yaml"
    text, count = re.subn(
        r"(?m)^name: .*$", f"name: {name}", chart_yaml.read_text(encoding="utf-8")
    )
    assert count == 1, "Chart.yaml has no single top-level name:"
    chart_yaml.write_text(text, encoding="utf-8")
    assert yaml.safe_load(chart_yaml.read_text())["name"] == name
    return copy


def render(chart: Path, release: str, values: str) -> List[Dict[str, Any]]:
    result = subprocess.run(
        [str(HELM), "template", release, str(chart), "-f", str(chart / "ci" / values)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def shape(docs: List[Dict[str, Any]]) -> Shape:
    """(kind, name) of every object; and per object what selects or names."""
    objects = {(d["kind"], d["metadata"]["name"]) for d in docs}
    detail: Dict[str, Any] = {}
    for d in docs:
        key = f"{d['kind']}/{d['metadata']['name']}"
        spec = d.get("spec") or {}
        labels = dict(d["metadata"].get("labels") or {})
        labels.pop("helm.sh/chart", None)
        detail[key] = {
            "labels": labels,
            "selector": spec.get("selector"),
            "template_labels": ((spec.get("template") or {}).get("metadata") or {}).get(
                "labels"
            ),
            "claims": [
                c["metadata"]["name"] for c in spec.get("volumeClaimTemplates") or []
            ],
            "service": spec.get("serviceName"),
        }
    return objects, detail


def describe(left: Shape, right: Shape) -> str:
    gone = sorted(f"{k}/{n}" for k, n in left[0] - right[0])
    new = sorted(f"{k}/{n}" for k, n in right[0] - left[0])
    return f"only before the rename: {gone}; only after it: {new}"


@pytest.mark.parametrize("values", CI_VALUES)
@pytest.mark.parametrize("release", RELEASES)
def test_a_renamed_chart_renders_the_same_objects(tmp_path, release, values):
    before = shape(render(CHART, release, values))
    after = shape(render(renamed_copy(tmp_path), release, values))
    assert after[0] == before[0], describe(before, after)
    assert after[1] == before[1]


def test_the_chart_label_still_names_the_chart(tmp_path):
    """The one thing that follows the chart's name: helm.sh/chart."""
    docs = render(renamed_copy(tmp_path), "experimently", CI_VALUES[0])
    charts = {d["metadata"]["labels"]["helm.sh/chart"] for d in docs}
    assert charts and all(c.startswith(f"{OTHER_NAME}-") for c in charts), charts


def test_the_comparison_fires_on_a_stem_read_from_the_chart_name(tmp_path):
    """The defect, planted: the fullname helper reads .Chart.Name again (the
    name helper alone pinned), and the comparison names the new postgres."""
    copy = renamed_copy(tmp_path)
    helpers = copy / "templates" / "_helpers.tpl"
    text = helpers.read_text(encoding="utf-8")
    pinned = '{{- $name := default "experimently" .Values.nameOverride }}'
    assert text.count(pinned) == 1
    helpers.write_text(
        text.replace(
            pinned, "{{- $name := default .Chart.Name .Values.nameOverride }}"
        ),
        encoding="utf-8",
    )
    before = shape(render(CHART, "experimently", CI_VALUES[0]))
    after = shape(render(copy, "experimently", CI_VALUES[0]))
    assert after[0] != before[0]
    assert ("StatefulSet", f"experimently-{OTHER_NAME}-postgres") in after[0]
