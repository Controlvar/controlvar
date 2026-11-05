"""Synthesise app.py without the CDK CLI, with the context the CLI would pass.

``cdk synth`` and ``cdk deploy`` run cdk.json's ``app`` (``python3 app.py``)
with the app's context in the environment variable ``CDK_CONTEXT_JSON``:
cdk.json's ``context`` -- the feature flags -- with every ``-c name=value`` on
the command line laid over it. app.py never reads cdk.json itself, so a bare
``python3 app.py`` synthesises without those flags: a template that no
deployment is ever given (#1299).

Use it for a synthesis whose templates have to be the ones a deployment is
given: the AWS installer's ``estimate`` and preflight, and the CDK tests in
test_synth_context.py and test_api_context_inputs.py. (Older CDK tests still
synthesise without it.) It imports only the standard library and calls no git,
no network and no AWS: the installer loads it by path, as it loads
stacks/alarm_email.py, and core_build's copy of the tree has no ``.git``.

Deliberately not passed:

* cdk.context.json (the lookups the CLI caches; ``{}`` in the repository) and
  the operator's ``~/.cdk.json``. A synthesis here does not depend on whose
  machine it runs on.
* The keys the CLI adds of its own: ``aws:cdk:enable-path-metadata``,
  ``aws:cdk:enable-asset-metadata``, ``aws:cdk:version-reporting``,
  ``aws:cdk:bundling-stacks`` and ``@aws-cdk/core:failSynthOnValidationErrors``
  (read in the source of CLI 2.1132.0). They are the CLI's settings, not the
  project's. Measured on staging's full profile, they add one
  ``AWS::CDK::Metadata`` resource per stack and ``aws:cdk:path`` metadata to
  resources, and change nothing else in any template.

A caller that cannot import this as a package loads it by path::

    spec = importlib.util.spec_from_file_location("synth_context", path)
    synth_context = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synth_context)
    result = synth_context.synthesize(
        outdir, environment, {"cors_origins": "http://localhost:3200"}
    )
    # result is a CompletedProcess; the assembly is in outdir.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

#: The directory holding app.py and cdk.json.
CDK_DIR = Path(__file__).resolve().parent

#: The project file whose ``context`` the CLI passes to the app.
CDK_JSON = CDK_DIR / "cdk.json"

#: The variable the CLI hands the app its context in, and the one naming the
#: directory the app writes its cloud assembly to (aws-cdk-lib's
#: ``cxapi.CONTEXT_ENV`` and ``cxapi.OUTDIR_ENV``).
CONTEXT_ENV = "CDK_CONTEXT_JSON"
OUTDIR_ENV = "CDK_OUTDIR"

#: A file of extra context the app reads when this variable names one
#: (``cxapi.CONTEXT_OVERFLOW_LOCATION_ENV``; the value of the constant is its
#: own name). The CLI uses it when the context is too large for the
#: environment. Removed from every environment built here, so the context is
#: exactly what :func:`context` returns.
CONTEXT_OVERFLOW_ENV = "CONTEXT_OVERFLOW_LOCATION_ENV"


def cdk_json_context(cdk_json: Path = CDK_JSON) -> dict:
    """cdk.json's ``context``, as written."""
    document = json.loads(Path(cdk_json).read_text(encoding="utf-8"))
    project_context = document.get("context")
    if not isinstance(project_context, dict):
        raise ValueError(f"{cdk_json} has no `context` object")
    return project_context


def context(
    inputs: Mapping[str, str] | None = None, *, cdk_json: Path = CDK_JSON
) -> dict:
    """The context for ``-c name=value ...``: cdk.json's, with each input laid
    over it, as the CLI lays them (less what the module docstring lists).

    A ``-c`` value is always a string, and the CLI refuses a name that starts
    with ``aws:``; so does this.
    """
    merged = dict(cdk_json_context(cdk_json))
    for name, value in (inputs or {}).items():
        if not isinstance(name, str) or not name or name.startswith("aws:"):
            raise ValueError(
                f"context name {name!r}: a -c name is a non-empty string that "
                "does not start with 'aws:'"
            )
        if not isinstance(value, str):
            raise TypeError(
                f"context {name}: a -c value is a string, not {type(value).__name__}"
            )
        merged[name] = value
    return merged


def app_environment(
    environment: Mapping[str, str],
    inputs: Mapping[str, str] | None = None,
    *,
    outdir: Path | str | None = None,
    cdk_json: Path = CDK_JSON,
) -> dict:
    """``environment``, plus the context, for a child ``python3 app.py``.

    The child gets exactly ``environment`` with CDK_CONTEXT_JSON set to
    :func:`context` (any value given is replaced), CONTEXT_OVERFLOW_ENV removed
    and, when ``outdir`` is given, CDK_OUTDIR set to it.
    """
    env = dict(environment)
    env.pop(CONTEXT_OVERFLOW_ENV, None)
    env[CONTEXT_ENV] = json.dumps(context(inputs, cdk_json=cdk_json))
    if outdir is not None:
        env[OUTDIR_ENV] = str(outdir)
    return env


def synthesize(
    outdir: Path | str,
    environment: Mapping[str, str],
    inputs: Mapping[str, str] | None = None,
    *,
    python: str = sys.executable,
    cdk_dir: Path = CDK_DIR,
    timeout: float = 900,
) -> subprocess.CompletedProcess:
    """Run ``<python> app.py`` in ``cdk_dir`` as the CLI runs cdk.json's ``app``.

    The child's environment is :func:`app_environment` of ``environment`` and
    ``inputs``, its cloud assembly is written to ``outdir``, and its output is
    captured. The result is returned whatever the exit status: a refused input
    is a non-zero exit with the refusal on stderr. ``python`` must be able to
    import aws_cdk; ``cdk_dir`` defaults to this file's directory (a copy of
    it, with no modules/ beside it, synthesises the core profile).
    """
    cdk_dir = Path(cdk_dir)
    return subprocess.run(
        [python, "app.py"],
        cwd=cdk_dir,
        env=app_environment(
            environment, inputs, outdir=outdir, cdk_json=cdk_dir / "cdk.json"
        ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
