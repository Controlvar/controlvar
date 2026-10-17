"""The Helm chart sets ``POSTGRES_SCHEMA`` from ``postgresql.schema``.

The API's configuration map carries ``POSTGRES_SCHEMA`` taken from the
release's ``postgresql.schema`` value, ``experimentation`` by default; with
the variable unset the application's own default is the same name
(``test_schema_name.py``). Reads the chart's files only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CHART = Path(__file__).resolve().parents[4] / "charts" / "experimently"


def test_the_chart_sets_the_schema_from_postgresql_schema():
    template = (CHART / "templates" / "config.yaml").read_text()
    assignments = re.findall(r"^\s*POSTGRES_SCHEMA:\s*(.+?)\s*$", template, re.M)
    assert assignments == ["{{ .Values.postgresql.schema | quote }}"], assignments
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["postgresql"]["schema"] == "experimentation"
    # Every API container reads that configuration map.
    deployment = (CHART / "templates" / "api-deployment.yaml").read_text()
    refs = re.findall(r"- configMapRef:\n\s+name: (.+?)\s*$", deployment, re.M)
    assert refs == ["{{ $fullname }}-config", "{{ $fullname }}-config"], refs
    assert '$fullname := include "controlvar.fullname" .' in deployment
    assert 'name: {{ include "controlvar.fullname" . }}-config' in template
    # Nothing else in the chart names the variable, so no container's own
    # `env:` overrides what the configuration map gives it.
    named = sorted(
        str(path.relative_to(CHART))
        for path in (CHART / "templates").rglob("*")
        if path.is_file() and "POSTGRES_SCHEMA" in path.read_text()
    )
    assert named == ["templates/config.yaml"], named
