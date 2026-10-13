"""The Java SDK's Maven build: its two modules, the Java it targets and the client's libraries.

Reads the POMs with the standard library's XML parser; Maven is not run.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
JAVA = REPO_ROOT / "sdk" / "java"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def pom(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def test_the_build_has_a_core_module_and_a_spring_boot_starter():
    parent = pom(JAVA / "pom.xml")
    assert [m.text for m in parent.findall("m:modules/m:module", NS)] == [
        "core",
        "spring-boot-starter",
    ]
    assert (
        JAVA
        / "spring-boot-starter"
        / "src"
        / "main"
        / "resources"
        / "META-INF"
        / "spring"
    ).is_dir()
    assert (
        parent.findtext("m:properties/m:maven.compiler.source", namespaces=NS) == "11"
    )
    assert (
        parent.findtext("m:properties/m:maven.compiler.target", namespaces=NS) == "11"
    )


def test_the_core_client_is_built_on_okhttp_and_jackson():
    core = pom(JAVA / "core" / "pom.xml")
    compile_scope = {
        d.findtext("m:artifactId", namespaces=NS)
        for d in core.findall("m:dependencies/m:dependency", NS)
        if d.findtext("m:scope", namespaces=NS) in (None, "compile")
    }
    assert {"okhttp", "jackson-databind"} <= compile_scope, compile_scope
