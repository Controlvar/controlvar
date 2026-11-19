"""The one way app.py is synthesised without the CDK CLI (#1299).

``cdk synth`` and ``cdk deploy`` run app.py with cdk.json's ``context`` in
CDK_CONTEXT_JSON; app.py never reads cdk.json. So a synthesis that runs app.py
any other way has to be given that context, or it builds templates that no
deployment is given: the feature flags change IAM policies and ARNs.
infrastructure/cdk/synth_context.py is that way, for the AWS installer and for
the tests here and in test_api_context_inputs.py, and these tests pin it:

* its context is cdk.json's ``context``, key for key, with each ``-c`` input
  laid over it as a string;
* a child app.py is given exactly that, in CDK_CONTEXT_JSON;
* a real synthesis reports every cdk.json flag it knows as set to cdk.json's
  value.

The helper is loaded by path, the way the installer loads it. The functions
at the bottom are the synthesis the other test file uses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CDK_DIR = REPO_ROOT / "infrastructure" / "cdk"
CDK_JSON = CDK_DIR / "cdk.json"
HELPER = CDK_DIR / "synth_context.py"
MODULES_PRESENT = (REPO_ROOT / "modules" / "infrastructure" / "cdk" / "stacks").is_dir()

#: The account and region of every synthesis here: the documented placeholder
#: the repository's account check accepts, never a real one.
FAKE_ACCOUNT = "111111111111"
FAKE_REGION = "us-west-2"


def load_helper():
    """synth_context.py, loaded by path as the installer loads it."""
    spec = importlib.util.spec_from_file_location("synth_context_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cdk_json_context() -> dict:
    """cdk.json's ``context``, read here rather than through the helper."""
    return json.loads(CDK_JSON.read_text(encoding="utf-8"))["context"]


# --- the context ----------------------------------------------------------------


@pytest.mark.regression
def test_the_context_is_cdk_json_context_key_for_key():
    """Every flag, with its value: dropping one would synthesise templates that
    no `cdk deploy` produces, and nothing else would say so."""
    expected = cdk_json_context()
    assert expected, "cdk.json has an empty context: this test checks nothing"
    helper = load_helper()
    assert helper.CDK_JSON == CDK_JSON
    given = helper.context()
    assert sorted(given) == sorted(expected), (
        f"missing {sorted(set(expected) - set(given))}, "
        f"extra {sorted(set(given) - set(expected))}"
    )
    assert given == expected


def test_each_input_is_laid_over_it_as_the_cli_lays_a_dash_c():
    """`-c name=value` adds a string, or replaces cdk.json's value; nothing is
    parsed, and a name the CLI refuses is refused."""
    expected = cdk_json_context()
    helper = load_helper()
    inputs = {"cors_origins": "http://localhost:3200", "backend_image_tag": "v1"}
    assert helper.context(inputs) == {**expected, **inputs}
    flag = sorted(expected)[0]
    assert helper.context({flag: "false"})[flag] == "false"
    for name in ("aws:cdk:enable-path-metadata", ""):
        with pytest.raises(ValueError):
            helper.context({name: "x"})
    for value in (1, True, None, ["a"]):
        with pytest.raises(TypeError):
            helper.context({"safety_check_interval_minutes": value})


#: The environment a caller hands the helper, with both other context sources
#: the app reads: a stale CDK_CONTEXT_JSON, and the overflow file the CLI can
#: name.
_STALE = {
    "PATH": os.environ.get("PATH", ""),
    "CDK_CONTEXT_JSON": json.dumps({"stale": True}),
    "CONTEXT_OVERFLOW_LOCATION_ENV": "/nonexistent/extra-context.json",
}


def test_the_child_environment_is_the_callers_plus_the_context():
    """Exactly the caller's variables, with CDK_CONTEXT_JSON replaced by the
    context, CDK_OUTDIR set, and the overflow file dropped."""
    helper = load_helper()
    inputs = {"cors_origins": "http://localhost:3200"}
    environment = helper.app_environment(_STALE, inputs, outdir="/tmp/assembly")
    assert sorted(environment) == ["CDK_CONTEXT_JSON", "CDK_OUTDIR", "PATH"]
    assert json.loads(environment["CDK_CONTEXT_JSON"]) == {
        **cdk_json_context(),
        **inputs,
    }
    assert environment["CDK_OUTDIR"] == "/tmp/assembly"
    assert environment["PATH"] == _STALE["PATH"]


#: A stand-in for app.py: it records what the CLI's child would be given.
_RECORDER = """
import json, os
from pathlib import Path
Path(os.environ["CDK_OUTDIR"], "given.json").write_text(json.dumps({
    "context": json.loads(os.environ["CDK_CONTEXT_JSON"]),
    "overflow": os.environ.get("CONTEXT_OVERFLOW_LOCATION_ENV"),
    "cwd": os.getcwd(),
}))
"""


def test_a_child_app_py_is_given_exactly_that_context(tmp_path):
    """The child gets the context in CDK_CONTEXT_JSON and runs in the app's
    directory, as the CLI runs cdk.json's `app`; neither stale source reaches
    it."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy(CDK_JSON, app_dir / "cdk.json")
    (app_dir / "app.py").write_text(_RECORDER, encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir()
    inputs = {"cors_origins": "http://localhost:3200,http://localhost:3300"}
    result = load_helper().synthesize(outdir, _STALE, inputs, cdk_dir=app_dir)
    assert result.returncode == 0, result.stderr
    given = json.loads((outdir / "given.json").read_text())
    assert given["context"] == {**cdk_json_context(), **inputs}
    assert given["overflow"] is None
    assert Path(given["cwd"]).resolve() == app_dir.resolve()


# --- the synthesis the tests use -------------------------------------------------


def synth_environment(env_name: str, home: Path, **extra: str) -> dict[str, str]:
    """Everything a child app.py is given besides its context, and nothing else.

    Built from nothing, as `env -i` would: the inputs app.py requires (fake
    account, region, certificate, public URL, alarm address and secret ARNs;
    no AWS call is made at synth), AWS credentials blanked, a scratch HOME,
    and this process's PATH so that jsii finds node. ``extra`` adds to it.
    """
    secret = (
        f"arn:aws:secretsmanager:{FAKE_REGION}:{FAKE_ACCOUNT}:secret:"
        f"/{env_name}/experimentation"
    )
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "ENVIRONMENT": env_name,
        "CDK_DEFAULT_ACCOUNT": FAKE_ACCOUNT,
        "CDK_DEFAULT_REGION": FAKE_REGION,
        "ALARM_EMAIL": "alerts@controlvar.test",
        "CERTIFICATE_ARN": (
            f"arn:aws:acm:{FAKE_REGION}:{FAKE_ACCOUNT}:certificate/"
            "11111111-2222-3333-4444-555555555555"
        ),
        "PUBLIC_BASE_URL": "https://install-test.controlvar.com",
        "JWT_SECRET_ARN": f"{secret}/jwt-secret-AAAAAA",
        "FIRST_SUPERUSER_PASSWORD_SECRET_ARN": (
            f"{secret}/first-superuser-password-BBBBBB"
        ),
        "AUDIT_HMAC_KEY_SECRET_ARN": f"{secret}/audit-hmac-key-CCCCCC",
        "AWS_ACCESS_KEY_ID": "",
        "AWS_SECRET_ACCESS_KEY": "",
        "AWS_SESSION_TOKEN": "",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        **extra,
    }


def core_copy(directory: Path) -> Path:
    """A copy of the CDK app with no modules/ beside it: a core checkout."""
    target = directory / "core-checkout" / "infrastructure" / "cdk"
    shutil.copytree(CDK_DIR, target, ignore=shutil.ignore_patterns("cdk.out"))
    return target


def run_synth(
    outdir: Path,
    env_name: str,
    *,
    cdk_dir: Path = CDK_DIR,
    inputs: dict[str, str] | None = None,
    exported: dict[str, str] | None = None,
    home: Path | None = None,
) -> subprocess.CompletedProcess:
    """app.py synthesised through the helper into ``outdir``, whatever the outcome.

    ``inputs`` are `-c` context; ``exported`` is added to the process
    environment. ``cdk_dir`` is CDK_DIR for the full profile (with modules/
    beside it) or :func:`core_copy` for the core profile. ``home`` is a scratch
    HOME, a new one if not given; synthesising one after another with the same
    one reuses jsii's package cache, which halves the time.
    """
    outdir.mkdir(parents=True)
    if home is None:
        home = outdir.parent / f"{outdir.name}-home"
        home.mkdir()
    return load_helper().synthesize(
        outdir,
        synth_environment(env_name, home, **(exported or {})),
        inputs,
        python=sys.executable,
        cdk_dir=cdk_dir,
    )


def synth(outdir: Path, env_name: str, **kwargs) -> Path:
    """:func:`run_synth`, which must succeed; returns the assembly directory."""
    result = run_synth(outdir, env_name, **kwargs)
    assert result.returncode == 0, (
        f"synthesis of {env_name} into {outdir.name} failed:\n{result.stderr[-3000:]}"
    )
    assert list(outdir.glob("*.template.json")), f"{outdir} holds no template"
    return outdir


@pytest.mark.regression
def test_a_real_synthesis_reports_every_flag_as_cdk_json_sets_it(tmp_path):
    """The app itself saw the context: its feature-flag report gives each flag
    it knows the value cdk.json sets. A synthesis with no context reports none
    of them as set (the PE's evidence was taken that way)."""
    expected = cdk_json_context()
    assembly = synth(tmp_path / "staging", "staging", cdk_dir=core_copy(tmp_path))
    manifest = json.loads((assembly / "manifest.json").read_text())
    (report,) = [
        artifact
        for artifact in manifest["artifacts"].values()
        if artifact.get("type") == "cdk:feature-flag-report"
    ]
    flags = report["properties"]["flags"]
    known = sorted(name for name in expected if name in flags)
    assert known, f"the report names none of cdk.json's flags: {sorted(flags)[:5]}"
    wrong = {
        name: flags[name].get("userValue")
        for name in known
        if flags[name].get("userValue") != expected[name]
    }
    assert not wrong, f"the app was given other values for these flags: {wrong}"
