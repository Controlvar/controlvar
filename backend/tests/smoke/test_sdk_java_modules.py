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


MODULE_POMS = ("core/pom.xml", "spring-boot-starter/pom.xml")


@pytest.mark.regression
def test_first_publish_manifests_are_0_1_0():
    """The Java SDK's first release on Maven Central is 0.1.0, a beta, like
    the JavaScript and Python SDKs' (#1498). The POMs said 1.0.0, a number
    never released. Edit this test with the version bump after 0.1.0: it pins
    the first publish, not every release."""
    assert pom(JAVA / "pom.xml").findtext("m:version", namespaces=NS) == "0.1.0"
    for relative in MODULE_POMS:
        child = pom(JAVA / relative)
        assert child.findtext("m:parent/m:version", namespaces=NS) == "0.1.0", relative
        assert child.find("m:version", NS) is None, (
            f"sdk/java/{relative} sets its own version; it takes the parent's"
        )


URL = "https://github.com/Controlvar/controlvar/tree/main/sdk/java#readme"
SCM_URL = "https://github.com/Controlvar/controlvar"
SCM_CONNECTION = "scm:git:https://github.com/Controlvar/controlvar.git"


def test_the_parent_pom_carries_what_maven_central_shows_for_good():
    """Central requires name, description, url, licences, developers and scm,
    and shows them with the version for good. The modules inherit url and scm
    unchanged: without the four `inherit.append.path` attributes Maven
    appends each module's artifactId, and core's url would be
    .../sdk/java#readme/controlvar-sdk (sdk-release.yml's maven-build job
    checks the effective values with Maven itself)."""
    parent = pom(JAVA / "pom.xml")
    assert parent.get("child.project.url.inherit.append.path") == "false"
    assert parent.findtext("m:url", namespaces=NS) == URL
    scm = parent.find("m:scm", NS)
    assert scm is not None
    for field in ("connection", "developerConnection", "url"):
        assert scm.get(f"child.scm.{field}.inherit.append.path") == "false", field
    assert scm.findtext("m:url", namespaces=NS) == SCM_URL
    assert scm.findtext("m:connection", namespaces=NS) == SCM_CONNECTION

    developers = parent.findall("m:developers/m:developer", NS)
    assert len(developers) == 1
    assert developers[0].findtext("m:name", namespaces=NS) == "Controlvar"
    assert developers[0].findtext("m:organizationUrl", namespaces=NS) == (
        "https://controlvar.com"
    )
    assert developers[0].find("m:email", NS) is None, "no personal email on Central"
    assert (
        parent.findtext("m:licenses/m:license/m:name", namespaces=NS) == "MIT License"
    )

    for relative in ("pom.xml", *MODULE_POMS):
        project = pom(JAVA / relative)
        assert (project.findtext("m:name", namespaces=NS) or "").strip(), relative
        assert (project.findtext("m:description", namespaces=NS) or "").strip(), (
            relative
        )
    for relative in MODULE_POMS:
        child = pom(JAVA / relative)
        for field in ("url", "scm", "developers", "licenses"):
            assert child.find(f"m:{field}", NS) is None, (
                f"sdk/java/{relative} sets its own {field}; it inherits the parent's"
            )


def test_the_starter_describes_what_it_does():
    """The description Central shows for the starter: it registers the client
    when `experimentation.api-key` is set. It said "zero-configuration ... via
    spring.factories"; there is no spring.factories, and nothing is
    registered without the key."""
    description = pom(JAVA / "spring-boot-starter" / "pom.xml").findtext(
        "m:description", namespaces=NS
    )
    assert "spring.factories" not in description
    assert "zero-configuration" not in description
    assert "experimentation.api-key" in description
    assert not list(
        (JAVA / "spring-boot-starter" / "src" / "main" / "resources").rglob(
            "spring.factories"
        )
    )


def test_the_contract_smoke_runner_stays_out_of_the_published_jars():
    """com.controlvar.sdk.examples (the live-contract runner, with a main())
    is not API: the jar, the sources jar and the javadoc leave it out, and
    examples/contract_smoke.sh runs it from core/target/classes instead."""
    core = pom(JAVA / "core" / "pom.xml")
    jar = [
        p
        for p in core.findall("m:build/m:plugins/m:plugin", NS)
        if p.findtext("m:artifactId", namespaces=NS) == "maven-jar-plugin"
    ]
    assert len(jar) == 1
    assert [
        e.text for e in jar[0].findall("m:configuration/m:excludes/m:exclude", NS)
    ] == ["com/controlvar/sdk/examples/**"]

    central = [
        p
        for p in pom(JAVA / "pom.xml").findall("m:profiles/m:profile", NS)
        if p.findtext("m:id", namespaces=NS) == "central"
    ]
    assert len(central) == 1
    plugins = {
        p.findtext("m:artifactId", namespaces=NS): p
        for p in central[0].findall("m:build/m:plugins/m:plugin", NS)
    }
    assert [
        e.text
        for e in plugins["maven-source-plugin"].findall(
            "m:configuration/m:excludes/m:exclude", NS
        )
    ] == ["com/controlvar/sdk/examples/**"]
    assert (
        plugins["maven-javadoc-plugin"].findtext(
            "m:configuration/m:excludePackageNames", namespaces=NS
        )
        == "com.controlvar.sdk.examples"
    )
    smoke = (JAVA / "examples" / "contract_smoke.sh").read_text(encoding="utf-8")
    assert 'java -cp "core/target/classes:core/target/lib/*"' in smoke
