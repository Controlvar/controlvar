"""The self-hosting CDK page's teardown commands are the synthesised import graph.

Split from ``test_environments_do_not_collide.py`` (#1142), which keeps the
synthesis and the graph and reads no documentation: these two tests compare
the commands docs/self-hosting/cdk.md gives with that graph.
"""

from __future__ import annotations

import re

import pytest

from .test_app_profiles import REPO_ROOT
from .test_environments_do_not_collide import (
    ENVIRONMENTS,
    MODULES_PRESENT,
    Synth,
    _exports,
    cluster_export_importers,
    import_graph,
)

CDK_DOC = REPO_ROOT / "docs" / "self-hosting" / "cdk.md"


@pytest.fixture(scope="module")
def synths() -> dict[str, Synth]:
    return {env: Synth(env) for env in ENVIRONMENTS}


def _doc_section(title: str) -> str:
    text = CDK_DOC.read_text()
    match = re.search(rf"^### {re.escape(title)}\n(.*?)(?=^##)", text, re.M | re.S)
    assert match, f"{CDK_DOC} has no section '### {title}'"
    return match.group(1)


def _commands(section: str, verb: str) -> list[str]:
    """The stacks named by ``cdk <verb>`` lines, in order, ``-<env>`` stripped."""
    return [
        m.group(1)
        for m in re.finditer(
            rf"^cdk {verb}(?: --\w+)* (experimentation-[a-z-]+?)-<env>\s*$",
            section,
            re.M,
        )
    ]


@pytest.mark.regression
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_rename_instruction_is_the_synthesised_import_graph(synths, env):
    """PE v2 condition 5: destroy fargate, deploy compute, redeploy fargate.

    The cluster's ``Ref`` is an export; CloudFormation refuses to change an
    export another stack imports, and renaming the cluster changes it. So the
    stacks to destroy first are exactly the importers of that export -- which
    the plan got wrong once (it named migrations and analytics, and analytics
    imports nothing from compute). Asserted from the synth, then against the
    documented commands.
    """
    importers = cluster_export_importers(synths[env])
    assert importers == {"experimentation-fargate"}, importers

    section = _doc_section("Moving an environment deployed before the rename")
    destroyed = _commands(section, "destroy")
    assert set(destroyed) == importers, (destroyed, importers)
    # --exclusively: without it the CLI also destroys the stacks that DEPEND
    # on fargate (the migrations stack), whose old template retains its log
    # group.
    assert re.search(
        r"^cdk destroy --exclusively experimentation-fargate-<env>$", section, re.M
    ), (
        "the fargate destroy must be --exclusively: the CDK CLI otherwise also "
        "destroys experimentation-migrations-<env>, which depends on it"
    )
    deployed = _commands(section, "deploy")
    assert deployed == ["experimentation-compute", "experimentation-fargate"], deployed

    # Nothing imports from fargate, so destroying it alone is allowed.
    fargate = f"experimentation-fargate-{env}"
    exports = _exports(synths[env])
    for name, importers_of in import_graph(synths[env]).items():
        if exports.get(name) == fargate:
            assert not importers_of, (name, importers_of)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_full_teardown_order_respects_imports_and_dependencies(synths, env):
    """Importers and dependants go first: monitoring and glue-etl before analytics."""
    synth = synths[env]
    order = _commands(_doc_section("Tearing an environment down"), "destroy")
    present = {synth.short(s) for s in synth.templates}
    assert set(order) == present | (
        set()
        if MODULES_PRESENT
        else {
            "experimentation-analytics",
            "experimentation-glue-etl",
            "experimentation-dynamodb-counters",
        }
    ), order
    position = dict(zip(order, range(len(order))))
    exports = _exports(synth)
    for name, importers in import_graph(synth).items():
        exporter = synth.short(exports[name])
        for importer in importers:
            assert position[synth.short(importer)] < position[exporter], (
                f"{importer} imports {name} from {exporter} and must be destroyed first"
            )
    for stack, dependencies in synth.dependencies.items():
        for dependency in dependencies:
            assert position[synth.short(stack)] < position[synth.short(dependency)], (
                f"{stack} depends on {dependency} and must be destroyed first"
            )
    if MODULES_PRESENT:
        assert (
            position["experimentation-monitoring"]
            < position["experimentation-analytics"]
        )
        assert (
            position["experimentation-glue-etl"] < position["experimentation-analytics"]
        )
