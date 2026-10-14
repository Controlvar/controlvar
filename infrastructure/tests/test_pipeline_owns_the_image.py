"""The pipeline owns the image the API service runs; CDK only bootstraps it.

`.github/workflows/deploy.yml` builds an image, rewrites the backend container
in the family's newest task definition to that image's DIGEST, registers that
as a new revision and hands the revision ARN to CodeDeploy.  CloudFormation is not
in that loop and cannot be: ECS refuses a task-definition change on a service
with a ``CODE_DEPLOY`` deployment controller ("Unable to update task definition
on services with a CODE_DEPLOY deployment controller").

What CloudFormation *can* still do is register a revision of its own.  The
service references the task definition **family**, and for a family with no
revision "the latest ACTIVE revision is used" -- so the newest revision is what
a forced new deployment, a CodeDeploy rollback or a task replacement would
run.  While the CDK's tag was ``latest`` that revision pointed at whatever the
deploy workflow had built most recently, including a build that failed
verification or was rolled back.

So the API service's tag must be one the pipeline never writes (#82).  The
migration task used to be the exception, on purpose: the deploy pushed
``:latest`` and ran the family by name, so ``latest`` meant "the image being
deployed" -- until another environment or profile built after it, since one
ECR repository serves them all and ECS resolves a tag at every task start
(#71, #138).  Now the deploy registers a migration revision naming the image by
DIGEST and runs that ARN, nothing pushes ``:latest``, and NO task definition
the app synthesises may name it.
"""

from __future__ import annotations

import json
import re
import runpy
from pathlib import Path

import aws_cdk as cdk
import pytest

from .test_app_profiles import CDK_DIR, _app_environment

#: The tag the deploy workflow moves on every build.  Nothing whose revision
#: can be consumed later may name it.
MUTABLE_TAG = "latest"


def _container_images(assembly) -> dict[tuple[str, str, str], str]:
    """``(stack, task definition logical id, container name) -> image``.

    The image of an *imported* ECR repository synthesises to an
    ``Fn::Join`` rather than a string, so it is compared as its JSON text.
    """
    images = {}
    for stack in assembly.stacks:
        for logical_id, resource in stack.template.get("Resources", {}).items():
            if resource["Type"] != "AWS::ECS::TaskDefinition":
                continue
            for container in resource["Properties"].get("ContainerDefinitions", []):
                images[(stack.stack_name, logical_id, container["Name"])] = json.dumps(
                    container.get("Image")
                )
    return images


@pytest.fixture(scope="module")
def assembly():
    with _app_environment(CDK_DIR):
        namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    return namespace["app"].synth()


#: What `-c backend_image_tag=sha-deadbeef` would put in the app's context.
PINNED_TAG = "sha-deadbeef"


@pytest.fixture(scope="module")
def pinned_assembly():
    """The app synthesised as if ``-c backend_image_tag=<tag>`` were given.

    ``app.py`` constructs its own ``App()``, and the CLI's ``-c`` arrives
    through ``CDK_CONTEXT_JSON``, which is read by the jsii kernel's Node
    process -- started at ``import aws_cdk``, so setting the variable later in
    this process does not reach it.  The context is therefore supplied the way
    the CLI ultimately supplies it, as an ``App`` prop.  What this pins is that
    the stack reads the key ``backend_image_tag``; CDK's own plumbing from
    ``-c`` to that context is CDK's to test.
    """
    original = cdk.App.__init__

    def with_pinned_tag(self, **kwargs):
        context = dict(kwargs.pop("context", None) or {})
        context["backend_image_tag"] = PINNED_TAG
        original(self, context=context, **kwargs)

    cdk.App.__init__ = with_pinned_tag
    try:
        with _app_environment(CDK_DIR):
            namespace = runpy.run_path(str(CDK_DIR / "app.py"), run_name="__main__")
    finally:
        cdk.App.__init__ = original
    return namespace["app"].synth()


@pytest.mark.regression
def test_the_scan_finds_the_task_definitions(assembly):
    """Guard against every assertion below passing on an empty scan.

    "No container uses :latest" and "the app synthesised nothing" look
    identical from the outside.
    """
    images = _container_images(assembly)
    assert len(images) >= 2, f"expected the API and migration containers, got {images}"
    names = {container for _, _, container in images}
    assert "backend" in names, f"no container named 'backend' anywhere: {sorted(names)}"


def _api_containers(assembly) -> dict[tuple[str, str, str], str]:
    """Just the API service's containers.

    Its own guard, because the filter is a substring of a stack name. Renaming
    the stack made this match nothing, and "no container uses `:latest`" and
    "no container was examined" are the same green -- the review tamper-tested
    exactly that and the assertion below stayed green.
    """
    found = {
        key: image
        for key, image in _container_images(assembly).items()
        if "fargate" in key[0]
    }
    assert found, (
        "no stack name contains 'fargate', so nothing below examined the API "
        f"service; the app synthesised {sorted({k[0] for k in _container_images(assembly)})}"
    )
    return found


@pytest.mark.regression
def test_the_api_service_does_not_run_a_mutable_tag(assembly):
    """#82: a `cdk deploy` must not register a revision naming `:latest`."""
    offenders = [
        (stack, logical_id, container, image)
        for (stack, logical_id, container), image in _api_containers(assembly).items()
        if f":{MUTABLE_TAG}" in image
    ]
    assert not offenders, (
        "the API service's task definition names a tag the deploy workflow "
        "moves on every build:\n"
        + "\n".join(f"  {s}/{lid}/{c}  {img}" for s, lid, c, img in offenders)
    )


#: Every task definition the app synthesises, by (stack prefix, container):
#: the API, the migration task and the dashboard. Exact, so a scan that finds
#: fewer -- a renamed stack, a moved container -- fails instead of passing.
EXPECTED_CONTAINERS = {
    ("experimentation-fargate", "backend"),
    ("experimentation-fargate", "dashboard"),
    ("experimentation-migrations", "backend"),
}


@pytest.mark.regression
def test_no_task_definition_names_a_mutable_tag(assembly):
    """#138: not the API, not the migration, not the dashboard.

    This replaces a test that pinned the migration task TO `:latest`
    (`test_the_migration_task_still_runs_the_tag_the_deploy_pushes`, added with
    #209 -- see #71). Its premise was that the deploy pushed `:latest` in the
    same job that ran the migration; but one ECR repository serves every
    environment and profile, so `latest` was whatever built last anywhere.
    """
    images = _container_images(assembly)
    found = {(stack.rsplit("-", 1)[0], container) for stack, _, container in images}
    assert found == EXPECTED_CONTAINERS, (
        f"the scan found {sorted(found)}; expected exactly "
        f"{sorted(EXPECTED_CONTAINERS)}, so it may have examined the wrong things"
    )
    offenders = [
        f"{stack}/{logical_id}/{container}  {image}"
        for (stack, logical_id, container), image in images.items()
        if f":{MUTABLE_TAG}" in image
    ]
    assert not offenders, (
        "a task definition names `:latest`, a tag any build can move:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


@pytest.mark.regression
def test_the_bootstrap_tag_is_overridable(pinned_assembly):
    """`-c backend_image_tag=<tag>` pins the revision CloudFormation registers.

    Without this, standing an environment back up after an infrastructure
    change could only ever register the bootstrap image.
    """
    backend = [
        image
        for (_, _, container), image in _api_containers(pinned_assembly).items()
        if container == "backend"
    ]
    assert backend, "no backend container in the fargate stack"
    assert all(PINNED_TAG in image for image in backend), (
        f"context did not reach the container image: {backend}"
    )


#: The deploy workflow, read as text. The contract `bootstrap` rests on is
#: that this file never writes that tag, and it lives in a different tree
#: from the stack that depends on it.
DEPLOY_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
)

BOOTSTRAP_TAG = "bootstrap"


@pytest.mark.skipif(
    not DEPLOY_WORKFLOW.is_file(), reason="this tree has no .github/workflows"
)
@pytest.mark.regression
def test_the_deploy_workflow_never_writes_the_bootstrap_tag():
    """The other half of the contract, and it is not in this tree.

    `bootstrap` is inert only because the pipeline never moves it. Adding
    `-t $ECR_REGISTRY/$ECR_BACKEND_REPO:bootstrap` to the build step restores
    #82 exactly -- the CloudFormation-registered revision follows every build
    again -- and every assertion above stays green, because they only ever
    read the synthesised templates.
    """
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(rf"\$ECR_BACKEND_REPO:{BOOTSTRAP_TAG}\b", line)
        or re.search(rf"push\s+\S*:{BOOTSTRAP_TAG}\b", line)
    ]
    assert not offenders, (
        "the deploy workflow writes the tag the CDK relies on never moving "
        "(#82):\n" + "\n".join(f"  {line}" for line in offenders)
    )
    # Vacuity guard: this file must be the one that pushes images at all.
    assert "$ECR_BACKEND_REPO:" in text, (
        f"{DEPLOY_WORKFLOW.name} pushes no backend image, so the check above "
        "is reading the wrong file"
    )


@pytest.mark.regression
def test_the_api_runs_from_experimentation_platform_backend_which_no_stack_creates(
    assembly,
):
    """The AWS CDK Deployment page: the API image comes from the ECR repository
    experimentation-platform/backend, which the CDK does not create."""
    backend = [
        image
        for (_, _, container), image in _container_images(assembly).items()
        if container == "backend"
    ]
    assert backend, "no backend container in the app"
    assert all("/experimentation-platform/backend:" in image for image in backend), (
        backend
    )
    created = [
        (stack.stack_name, lid)
        for stack in assembly.stacks
        for lid, r in stack.template.get("Resources", {}).items()
        if r["Type"] == "AWS::ECR::Repository"
    ]
    assert not created, created
