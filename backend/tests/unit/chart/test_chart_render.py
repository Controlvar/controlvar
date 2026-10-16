"""The Helm chart's render contract (charts/experimently).

``helm lint`` and ``helm template`` never validate a manifest against the
Kubernetes API, and kubeconform only checks shapes; neither knows what this
application needs. These tests render the chart with the real ``helm`` and
assert on the parsed manifests:

* the images are ``<profile>-<appVersion>`` and appVersion is ``VERSION``;
* secrets reach the pods only through ``secretKeyRef``;
* migrations run in the ``migrate`` init container and never in ``api``;
  ``SEED`` appears nowhere;
* the probes are the paths the API serves, and the Ingress host is
  ``publicBaseUrl``'s hostname;
* the amd64 nodeSelector is on the pods running our images only;
* every schema refusal fails the render, naming the value's path;
* the environment the chart gives the API is enough for the application to
  start in production -- derived from the settings validators themselves and
  from the Docker Smoke production list, not from a third list typed here.

The module runs only with ``EXPERIMENTLY_CHART_TESTS=1``, which only the
``chart`` workflow sets, next to the helm it pins. Elsewhere it skips: the
Unit Tests job collects this directory too, and would otherwise assert on
whatever helm the runner image ships (its error wording, its schema engine),
coupling every unrelated pull request to a runner upgrade. The ``chart`` job
fails on any skip here (``scripts/check_junit_skips.py``), so a lost variable
or a missing helm cannot pass for green there.

Locally: ``EXPERIMENTLY_CHART_TESTS=1 HELM=/path/to/helm-3.19.0 pytest
backend/tests/unit/chart``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List
from urllib.parse import urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
CHART = REPO_ROOT / "charts" / "experimently"
CI_VALUES = {
    "core": CHART / "ci" / "values-core.yaml",
    "full": CHART / "ci" / "values-full.yaml",
}
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
PR_QA_GATE = REPO_ROOT / ".github" / "workflows" / "pr-qa-gate.yml"
RELEASE = "exp"
FULLNAME = f"{RELEASE}-experimently"
PUBLIC_BASE_URL = "https://controlvar.example.com"

API_REPOSITORY = "ghcr.io/controlvar/controlvar"
WEB_REPOSITORY = "ghcr.io/controlvar/controlvar-web"

#: Every value the chart must never carry in plain text.
SECRET_NAMES = {
    "SECRET_KEY",
    "FIRST_SUPERUSER_PASSWORD",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "AUDIT_HMAC_KEY",
    "REDIS_PASSWORD",
    "METRICS_TOKEN",
}

HELM = os.environ.get("HELM") or shutil.which("helm")
ENABLED = os.environ.get("EXPERIMENTLY_CHART_TESTS") == "1"

pytestmark = [
    pytest.mark.unit,
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


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HELM), "template", RELEASE, str(CHART), *args],
        capture_output=True,
        text=True,
    )


def _sets(overrides: Dict[str, str]) -> List[str]:
    args: List[str] = []
    for key, value in overrides.items():
        args += [
            "--set-string" if isinstance(value, str) else "--set",
            f"{key}={value}",
        ]
    return args


def render(profile: str = "core", *extra: str) -> List[Dict[str, Any]]:
    """The manifests for a CI values file plus *extra* helm arguments."""
    result = _helm("-f", str(CI_VALUES[profile]), *extra)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def refused(profile: str, *extra: str) -> str:
    """Assert the render fails; return its error text."""
    result = _helm("-f", str(CI_VALUES[profile]), *extra)
    assert result.returncode != 0, (
        f"helm template accepted {extra!r}; it must refuse it. Output:\n{result.stdout[:2000]}"
    )
    return result.stderr


def one(docs: List[Dict[str, Any]], kind: str, name: str) -> Dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected one {kind} {name}, found {len(found)}"
    return found[0]


def pod_spec(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc["spec"] if doc["kind"] == "Pod" else doc["spec"]["template"]["spec"]


def workloads(docs: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    for doc in docs:
        if doc["kind"] in ("Deployment", "StatefulSet", "Pod", "Job", "DaemonSet"):
            yield doc


def containers(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = pod_spec(doc)
    return list(spec.get("initContainers") or []) + list(spec["containers"])


def container(doc: Dict[str, Any], name: str) -> Dict[str, Any]:
    matches = [c for c in containers(doc) if c["name"] == name]
    assert len(matches) == 1, (
        f"{doc['metadata']['name']}: {len(matches)} containers named {name}"
    )
    return matches[0]


def env_of(c: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {e["name"]: e for e in c.get("env") or []}


def api_environment(
    docs: List[Dict[str, Any]], container_name: str = "api"
) -> Dict[str, Any]:
    """Name -> literal value (or the secretKeyRef dict) the API container sees."""
    api = container(one(docs, "Deployment", f"{FULLNAME}-api"), container_name)
    environment: Dict[str, Any] = {}
    for source in api.get("envFrom") or []:
        ref = source.get("configMapRef")
        assert ref, f"unexpected envFrom source {source}"
        environment.update(one(docs, "ConfigMap", ref["name"])["data"])
    for name, entry in env_of(api).items():
        environment[name] = entry.get("value", entry.get("valueFrom"))
    return environment


@pytest.fixture(scope="module", params=["core", "full"])
def profile(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def docs(profile: str) -> List[Dict[str, Any]]:
    return render(profile)


# ---------------------------------------------------------------------------
# version and images
# ---------------------------------------------------------------------------


def test_chart_version_and_app_version_equal_VERSION():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["version"] == VERSION, (chart["version"], VERSION)
    assert chart["appVersion"] == VERSION, (chart["appVersion"], VERSION)
    assert "dependencies" not in chart, "the chart carries no subcharts"


def test_images_are_profile_dash_app_version(docs, profile):
    api = f"{API_REPOSITORY}:{profile}-{VERSION}"
    web = f"{WEB_REPOSITORY}:{profile}-{VERSION}"
    deployment = one(docs, "Deployment", f"{FULLNAME}-api")
    assert container(deployment, "migrate")["image"] == api
    assert container(deployment, "api")["image"] == api
    assert container(one(docs, "Deployment", f"{FULLNAME}-web"), "web")["image"] == web
    assert container(one(docs, "Pod", f"{FULLNAME}-smoke"), "smoke")["image"] == api


def test_a_digest_pins_the_image():
    digest = "sha256:" + "a" * 64
    docs = render("core", "--set", f"image.api.digest={digest}")
    api = container(one(docs, "Deployment", f"{FULLNAME}-api"), "api")
    assert api["image"] == f"{API_REPOSITORY}@{digest}"


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


def test_secrets_reach_pods_only_by_secret_key_ref(docs):
    for workload in workloads(docs):
        for c in containers(workload):
            for source in c.get("envFrom") or []:
                assert "secretRef" not in source, (
                    f"{workload['metadata']['name']}/{c['name']} takes a whole Secret "
                    "through envFrom; name each key through secretKeyRef instead"
                )
            for name, entry in env_of(c).items():
                if name in SECRET_NAMES:
                    assert "value" not in entry, (
                        f"{workload['metadata']['name']}/{c['name']} has {name} as a literal value"
                    )
                    assert "secretKeyRef" in entry["valueFrom"], (
                        workload["metadata"]["name"],
                        name,
                    )
    for config_map in (d for d in docs if d["kind"] == "ConfigMap"):
        leaked = SECRET_NAMES & set(config_map["data"])
        assert not leaked, (
            f"ConfigMap {config_map['metadata']['name']} carries {sorted(leaked)}"
        )


def test_audit_hmac_key_is_required_exactly_when_full(docs, profile):
    environment = api_environment(docs)
    if profile == "full":
        assert "AUDIT_HMAC_KEY" in environment
        assert "optional" not in environment["AUDIT_HMAC_KEY"]["secretKeyRef"]
    else:
        assert "AUDIT_HMAC_KEY" not in environment


def test_an_existing_secret_means_the_chart_renders_none():
    docs = render("full")
    assert not [d for d in docs if d["kind"] == "Secret"]
    ref = api_environment(docs)["SECRET_KEY"]["secretKeyRef"]
    assert ref["name"] == "experimently-secrets"


@pytest.mark.parametrize(
    "key, profile_",
    [
        ("secretKey", "core"),
        ("firstSuperuserPassword", "core"),
        ("postgresUser", "core"),
        ("postgresPassword", "core"),
        ("auditHmacKey", "full"),
    ],
)
def test_a_missing_secret_stops_the_render_and_names_it(key, profile_):
    # Nothing is generated: without the value the render fails, naming it.
    overrides = {
        "profile": profile_,
        "secrets.auditHmacKey": "a" * 64,
        f"secrets.{key}": "",
    }
    error = refused("core", *_sets(overrides))
    assert f"secrets.{key} is required" in error, error


# ---------------------------------------------------------------------------
# migrations and seeds
# ---------------------------------------------------------------------------


def test_migrations_run_in_the_init_container_only(docs):
    deployment = one(docs, "Deployment", f"{FULLNAME}-api")
    init = pod_spec(deployment)["initContainers"]
    assert [c["name"] for c in init] == ["migrate"]
    migrate = init[0]
    assert migrate["args"] == ["true"]
    assert "command" not in migrate, "the image entrypoint is what runs the bootstrap"
    assert env_of(migrate)["RUN_MIGRATIONS"]["value"] == "true"
    assert env_of(container(deployment, "api"))["RUN_MIGRATIONS"]["value"] == "false"


#: What the chart sets as explicit `env` on BOTH API containers. Explicit env
#: wins over every envFrom source, so api.extraEnvFrom cannot override these;
#: a value arriving through the chart's own ConfigMap could be (a later
#: envFrom source overrides an earlier one).
PINNED_ENV = {
    "ENVIRONMENT": "production",
    "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
    "SEED": "",
    "SEED_FORCE": "",
}


@pytest.mark.parametrize("with_extra_env_from", [False, True])
def test_pinned_env_is_explicit_on_both_api_containers(profile, with_extra_env_from):
    extra = (
        ["--set-string", "api.extraEnvFrom[0].configMapRef.name=operator-env"]
        if with_extra_env_from
        else []
    )
    docs = render(profile, *extra)
    deployment = one(docs, "Deployment", f"{FULLNAME}-api")
    for name in ("migrate", "api"):
        entries = env_of(container(deployment, name))
        for key, value in PINNED_ENV.items():
            assert key in entries, f"{name} has no explicit {key} entry"
            assert entries[key].get("value") == value, (name, key, entries[key])
            assert "valueFrom" not in entries[key], (name, key)
        if with_extra_env_from:
            sources = container(deployment, name)["envFrom"]
            assert {"configMapRef": {"name": "operator-env"}} in sources, sources
    for config_map in (d for d in docs if d["kind"] == "ConfigMap"):
        assert not set(PINNED_ENV) & set(config_map["data"]), (
            f"ConfigMap {config_map['metadata']['name']} carries a pinned variable; "
            "an extraEnvFrom source listed after it would override it"
        )


def test_seed_is_set_by_nothing_else(docs):
    # Only the two API containers mention SEED at all, and only as "".
    for workload in workloads(docs):
        for c in containers(workload):
            if workload["metadata"]["name"] == f"{FULLNAME}-api":
                continue
            assert not {"SEED", "SEED_FORCE"} & set(env_of(c)), (
                workload["metadata"]["name"],
                c["name"],
            )


@pytest.mark.parametrize(
    "name", ["SEED", "SEED_FORCE", "RUN_MIGRATIONS", "ENVIRONMENT", "PUBLIC_BASE_URL"]
)
def test_extra_env_cannot_reintroduce_what_the_chart_pins(name):
    error = refused(
        "core",
        "--set-string",
        f"api.extraEnv[0].name={name}",
        "--set-string",
        "api.extraEnv[0].value=true",
    )
    assert f"api.extraEnv may not set {name}" in error, error


# ---------------------------------------------------------------------------
# probes, routing, scheduling, security
# ---------------------------------------------------------------------------


def test_probe_paths(docs):
    api = container(one(docs, "Deployment", f"{FULLNAME}-api"), "api")
    assert api["startupProbe"]["httpGet"]["path"] == "/health/live"
    assert api["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert api["livenessProbe"]["httpGet"]["path"] == "/health/live"
    web = container(one(docs, "Deployment", f"{FULLNAME}-web"), "web")
    assert web["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_the_dashboard_proxies_to_the_api_service(docs):
    web = container(one(docs, "Deployment", f"{FULLNAME}-web"), "web")
    assert env_of(web)["API_UPSTREAM"]["value"] == f"http://{FULLNAME}-api:8000"
    service = one(docs, "Service", f"{FULLNAME}-api")
    assert [p["port"] for p in service["spec"]["ports"]] == [8000]


@pytest.mark.parametrize(
    "url",
    ["https://controlvar.example.com", "http://flags.internal.example.org"],
)
def test_the_ingress_host_is_the_public_base_url_hostname(url):
    docs = render("core", "--set-string", f"publicBaseUrl={url}")
    ingress = one(docs, "Ingress", FULLNAME)
    rules = ingress["spec"]["rules"]
    assert [r["host"] for r in rules] == [urlparse(url).hostname]
    routes = {
        p["path"]: (p["pathType"], p["backend"]["service"]["name"])
        for p in rules[0]["http"]["paths"]
    }
    assert routes == {
        "/api": ("Prefix", f"{FULLNAME}-api"),
        "/ws": ("Prefix", f"{FULLNAME}-api"),
        "/health": ("Prefix", f"{FULLNAME}-api"),
        "/": ("Prefix", f"{FULLNAME}-web"),
    }
    assert api_environment(docs)["PUBLIC_BASE_URL"] == url
    config = one(docs, "ConfigMap", f"{FULLNAME}-config")["data"]
    assert "ALLOWED_HOSTS" not in config, (
        "the API derives its allow-list from PUBLIC_BASE_URL"
    )


def test_helm_test_checks_the_public_host_and_refuses_another(docs):
    """`helm test`, which the install notes and the README name: the API
    answers 200 for publicBaseUrl's host and 400 for any other."""
    smoke = one(docs, "Pod", f"{FULLNAME}-smoke")
    assert smoke["metadata"]["annotations"]["helm.sh/hook"] == "test"
    test = container(smoke, "smoke")
    assert env_of(test)["PUBLIC_HOST"]["value"] == urlparse(PUBLIC_BASE_URL).hostname
    script = test["args"][0]
    assert '-H "Host: $PUBLIC_HOST" "$BASE/api/v1/modules"' in script
    assert '[ "$code" = 200 ]' in script
    assert '-H "Host: not-$PUBLIC_HOST" "$BASE/api/v1/modules"' in script
    assert '[ "$code" = 400 ]' in script


def test_the_bundled_postgres_and_redis_are_one_replica_each_and_never_backed_up(docs):
    """The core CI values keep the chart's defaults, which bundle both, each a
    single replica; no values render a job of any kind, so nothing backs the
    database up."""
    defaults = render("core")
    postgres = one(defaults, "StatefulSet", f"{FULLNAME}-postgres")
    redis = one(defaults, "Deployment", f"{FULLNAME}-redis")
    assert postgres["spec"]["replicas"] == 1
    assert redis["spec"]["replicas"] == 1
    # One major version each, fixed by the chart: an upgrade never moves the
    # data to another one.
    assert container(postgres, "postgres")["image"] == "postgres:16-alpine"
    assert container(redis, "redis")["image"] == "redis:7-alpine"
    assert [d["kind"] for d in docs if d["kind"] in ("CronJob", "Job")] == []


#: Every object, by (kind, name), that the chart rendered for each release
#: before it was renamed (charts/experimently at 1303435c, ci/values-core.yaml).
#: Measured, not derived from anything the chart now says.
OBJECTS_BEFORE_THE_RENAME = {
    release: sorted(
        (kind, f"{prefix}{suffix}")
        for kind, suffix in (
            ("ConfigMap", "-config"),
            ("Deployment", "-api"),
            ("Deployment", "-redis"),
            ("Deployment", "-web"),
            ("Ingress", ""),
            ("PersistentVolumeClaim", "-redis"),
            ("Pod", "-smoke"),
            ("Secret", "-secrets"),
            ("Service", "-api"),
            ("Service", "-postgres"),
            ("Service", "-redis"),
            ("Service", "-web"),
            ("ServiceAccount", ""),
            ("StatefulSet", "-postgres"),
        )
    )
    for release, prefix in (
        ("experimently", "experimently"),
        ("foo", "foo-experimently"),
    )
}


@pytest.mark.regression
@pytest.mark.parametrize("release", sorted(OBJECTS_BEFORE_THE_RENAME))
def test_chart_rename_keeps_every_object_name(release):
    """Renaming the chart renames no object. Its names and selector labels
    come from the stem `experimently`, not from Chart.yaml's `name:`; were they
    taken from the name, `helm upgrade` would create a new PostgreSQL
    StatefulSet with a new, empty volume and leave the data on the old one,
    and every pod would still come up Ready."""
    result = subprocess.run(
        [str(HELM), "template", release, str(CHART), "-f", str(CI_VALUES["core"])],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    names = sorted((d["kind"], d["metadata"]["name"]) for d in docs)
    assert names == OBJECTS_BEFORE_THE_RENAME[release]
    for doc in docs:
        labels = doc["metadata"].get("labels", {})
        if "app.kubernetes.io/name" in labels:
            assert labels["app.kubernetes.io/name"] == "experimently", doc["metadata"]


@pytest.mark.parametrize(
    "release, prefix",
    [
        ("experimently", "experimently"),
        ("prod", "prod-experimently"),
        ("my-experimently", "my-experimently"),
    ],
)
def test_the_resources_are_named_after_the_release(release, prefix):
    """The guide installs release `experimently`, whose resources are
    `experimently-api` and `experimently-web`: a release name that contains the
    resource stem, `experimently`, is used as it is, any other gets
    `-experimently` added. The stem is not the chart's name (see below)."""
    result = subprocess.run(
        [str(HELM), "template", release, str(CHART), "-f", str(CI_VALUES["core"])],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    for kind in ("Deployment", "Service"):
        one(docs, kind, f"{prefix}-api")
        one(docs, kind, f"{prefix}-web")
    api = one(docs, "Deployment", f"{prefix}-api")
    assert [c["name"] for c in pod_spec(api)["initContainers"]] == ["migrate"]
    smoke = one(docs, "Pod", f"{prefix}-smoke")
    assert (
        env_of(container(smoke, "smoke"))["BASE"]["value"]
        == f"http://{prefix}-api:8000"
    )


def test_extra_allowed_hosts_keep_the_public_host():
    config = one(render("full"), "ConfigMap", f"{FULLNAME}-config")["data"]
    assert config["ALLOWED_HOSTS"].split(",") == [
        urlparse(PUBLIC_BASE_URL).hostname,
        "*.internal.example.com",
    ]


def test_no_cors_origins_by_default(docs):
    config = one(docs, "ConfigMap", f"{FULLNAME}-config")["data"]
    assert "CORS_ORIGINS" not in config, (
        "the dashboard is served from publicBaseUrl and needs no CORS entry"
    )


def test_cors_origins_reach_the_api_as_one_list():
    docs = render(
        "core",
        "--set-string",
        "corsOrigins[0]=https://shop.example.com",
        "--set-string",
        "corsOrigins[1]=http://localhost:3100",
    )
    config = one(docs, "ConfigMap", f"{FULLNAME}-config")["data"]
    assert config["CORS_ORIGINS"] == "https://shop.example.com,http://localhost:3100"


@pytest.mark.parametrize(
    "url", ["http://10.0.0.5", "https://controlvar.example.com:8443", "http://[::1]"]
)
def test_an_ip_or_port_origin_is_refused_with_the_ingress_on(url):
    error = refused("core", "--set-string", f"publicBaseUrl={url}")
    assert "/publicBaseUrl" in error, error
    # Without the schema, the template refuses too, and says what to do.
    error = refused(
        "core", "--skip-schema-validation", "--set-string", f"publicBaseUrl={url}"
    )
    assert "ingress.enabled=false" in error, error


def test_an_ip_origin_is_accepted_with_the_ingress_off():
    docs = render(
        "core",
        "--set-string",
        "publicBaseUrl=http://10.0.0.5:8080",
        "--set",
        "ingress.enabled=false",
    )
    assert not [d for d in docs if d["kind"] == "Ingress"]


def test_amd64_node_selector_is_on_our_images_only():
    docs = render("core")
    ours = {
        ("Deployment", f"{FULLNAME}-api"),
        ("Deployment", f"{FULLNAME}-web"),
        ("Pod", f"{FULLNAME}-smoke"),
    }
    seen = set()
    for workload in workloads(docs):
        key = (workload["kind"], workload["metadata"]["name"])
        selector = pod_spec(workload).get("nodeSelector") or {}
        if key in ours:
            assert selector.get("kubernetes.io/arch") == "amd64", key
            seen.add(key)
        else:
            # postgres:16-alpine and redis:7-alpine are multi-arch; pinning
            # them to amd64 would stop an arm64-only cluster running them.
            assert "kubernetes.io/arch" not in selector, key
    assert seen == ours
    assert {
        ("StatefulSet", f"{FULLNAME}-postgres"),
        ("Deployment", f"{FULLNAME}-redis"),
    } <= {(w["kind"], w["metadata"]["name"]) for w in workloads(docs)}


@pytest.mark.parametrize(
    "name, uid",
    [
        (f"{FULLNAME}-api", 1001),
        (f"{FULLNAME}-web", 101),
        (f"{FULLNAME}-postgres", 70),
        (f"{FULLNAME}-redis", 999),
        (f"{FULLNAME}-smoke", 1001),
    ],
)
def test_pods_run_as_numeric_non_root_users_on_a_read_only_root(name, uid):
    docs = render("core")
    (workload,) = [w for w in workloads(docs) if w["metadata"]["name"] == name]
    spec = pod_spec(workload)
    assert spec["securityContext"]["runAsUser"] == uid
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["automountServiceAccountToken"] is False
    pod_seccomp = (spec["securityContext"].get("seccompProfile") or {}).get("type")
    for c in containers(workload):
        context = c["securityContext"]
        assert context["readOnlyRootFilesystem"] is True, (name, c["name"])
        assert context["allowPrivilegeEscalation"] is False, (name, c["name"])
        assert context.get("capabilities", {}).get("drop") == ["ALL"], (
            name,
            c["name"],
            context.get("capabilities"),
        )
        # Set on the pod or on the container; the container's wins.
        seccomp = (context.get("seccompProfile") or {}).get("type", pod_seccomp)
        assert seccomp == "RuntimeDefault", (name, c["name"], seccomp)


def test_every_pod_is_covered_by_the_security_test():
    # The parametrised list above must name every workload the chart renders.
    covered = {
        f"{FULLNAME}-api",
        f"{FULLNAME}-web",
        f"{FULLNAME}-postgres",
        f"{FULLNAME}-redis",
        f"{FULLNAME}-smoke",
    }
    assert {w["metadata"]["name"] for w in workloads(render("core"))} == covered


def test_service_account_token_is_not_mounted(docs):
    account = one(docs, "ServiceAccount", FULLNAME)
    assert account["automountServiceAccountToken"] is False


def test_pod_disruption_budget_only_with_more_than_one_replica():
    assert not [d for d in render("core") if d["kind"] == "PodDisruptionBudget"]
    budget = one(render("full"), "PodDisruptionBudget", f"{FULLNAME}-api")
    assert budget["spec"]["minAvailable"] == 1


# ---------------------------------------------------------------------------
# schema refusals: each must fail the render and name the value's path
# ---------------------------------------------------------------------------

REFUSALS = [
    ("empty publicBaseUrl", {"publicBaseUrl": ""}, "/publicBaseUrl"),
    ("wildcard origin", {"publicBaseUrl": "https://*.example.com"}, "/publicBaseUrl"),
    (
        "origin with a path",
        {"publicBaseUrl": "https://x.example.com/app"},
        "/publicBaseUrl",
    ),
    ("trailing slash", {"publicBaseUrl": "https://x.example.com/"}, "/publicBaseUrl"),
    ("no scheme", {"publicBaseUrl": "x.example.com"}, "/publicBaseUrl"),
    ("IPv4 origin, Ingress on", {"publicBaseUrl": "http://10.0.0.5"}, "/publicBaseUrl"),
    (
        "port, Ingress on",
        {"publicBaseUrl": "https://x.example.com:8443"},
        "/publicBaseUrl",
    ),
    ("profile typo", {"profile": "fulll"}, "/profile"),
    ("unknown top-level key", {"postgress.host": "x"}, "'postgress' not allowed"),
    ("unknown nested key", {"api.replicas": 2}, "'replicas' not allowed"),
    (
        "existingSecret with an inline secret",
        {"existingSecret": "s"},
        "/secrets/secretKey",
    ),
    ("zero API replicas", {"api.replicaCount": 0}, "/api/replicaCount"),
    ("zero web replicas", {"web.replicaCount": 0}, "/web/replicaCount"),
    ("floating tag", {"image.tag": "core"}, "/image/tag"),
    ("latest tag", {"image.tag": "latest"}, "/image/tag"),
    ("tag profile mismatch", {"image.tag": f"full-{VERSION}"}, "/image/tag"),
    ("allowedHosts *", {"allowedHosts[0]": "*"}, "/allowedHosts/0"),
    (
        "allowedHosts *example.com",
        {"allowedHosts[0]": "*example.com"},
        "/allowedHosts/0",
    ),
    ("corsOrigins *", {"corsOrigins[0]": "*"}, "/corsOrigins/0"),
    (
        "corsOrigins wildcard host",
        {"corsOrigins[0]": "https://*.example.com"},
        "/corsOrigins/0",
    ),
    (
        "corsOrigins with a path",
        {"corsOrigins[0]": "https://shop.example.com/"},
        "/corsOrigins/0",
    ),
    ("no first superuser", {"firstSuperuser": ""}, "/firstSuperuser"),
    (
        "sslmode verify-full",
        {"postgresql.sslMode": "verify-full"},
        "/postgresql/sslMode",
    ),
    ("digest not sha256", {"image.api.digest": "md5:abc"}, "/image/api/digest"),
]


@pytest.mark.parametrize(
    "overrides, path",
    [(o, p) for _, o, p in REFUSALS],
    ids=[label for label, _, _ in REFUSALS],
)
def test_schema_refuses(overrides, path):
    error = refused("core", *_sets(overrides))
    assert "values don't meet the specifications of the schema" in error, error
    assert path in error, error


def test_both_ci_values_files_pass_the_schema():
    # The refusals above mean something only if the baseline is accepted.
    for profile_ in CI_VALUES:
        render(profile_)


# ---------------------------------------------------------------------------
# environment drift: the chart gives the API what production demands
# ---------------------------------------------------------------------------


def _docker_smoke_production_names() -> Dict[str, set]:
    """The variables Docker Smoke's "Full image boots with production settings" sets.

    ``prod=( -e NAME=... )`` is what every production start gets; a further
    ``-e NAME=`` on the ``docker run`` line is what the full profile adds.
    """
    if not PR_QA_GATE.is_file():
        pytest.skip("this tree has no .github/workflows/pr-qa-gate.yml")
    text = PR_QA_GATE.read_text()
    step = re.search(
        r"- name: Full image boots with production settings\n(.*?)\n      - name:",
        text,
        re.S,
    )
    assert step, (
        "pr-qa-gate.yml has no 'Full image boots with production settings' step"
    )
    body = step.group(1)
    array = re.search(r"prod=\((.*?)\n\s*\)", body, re.S)
    assert array, "the step no longer builds a prod=( ... ) array"
    everywhere = set(re.findall(r"-e ([A-Z][A-Z0-9_]*)=", array.group(1)))
    run_line = re.search(r'docker run --rm "\$\{prod\[@\]\}"([^\n]*)', body)
    assert run_line, "the step no longer runs the image with ${prod[@]}"
    full_only = set(re.findall(r"-e ([A-Z][A-Z0-9_]*)=", run_line.group(1)))
    assert "ENVIRONMENT" in everywhere and full_only, (everywhere, full_only)
    return {"core": everywhere, "full": everywhere | full_only}


def test_chart_env_covers_the_docker_smoke_production_list(profile):
    required = _docker_smoke_production_names()[profile]
    # Docker Smoke's list is a deployment WITH Redis (the ECS task's), so it is
    # compared with Redis on. The full CI case runs without Redis, which the
    # settings-validator test below covers.
    docs = render(profile, "--set", "redis.enabled=true", "--set", "redis.bundled=true")
    missing = required - set(api_environment(docs))
    assert not missing, f"the chart omits production-required {sorted(missing)}"
    missing = required - set(api_environment(docs, "migrate"))
    assert not missing, (
        f"the migrate container omits production-required {sorted(missing)}"
    )


_CORE_SETTINGS = """
import backend.app.core.config as config
assert config.settings.ENVIRONMENT == "production", config.settings.ENVIRONMENT
print("core settings built")
"""

_MODULE_SETTINGS = (
    _CORE_SETTINGS
    + """
from modules.backend.app.settings import build_modules_settings
build_modules_settings()
print("modules settings built")
"""
)


def _environment_for_settings(
    docs: List[Dict[str, Any]], container_name: str
) -> Dict[str, str]:
    """What the container would see: ConfigMap values as rendered, and a
    strong stand-in for each secret the chart requires. Optional secret keys
    are left out -- a real install may not have them."""
    environment: Dict[str, str] = {}
    for name, value in api_environment(docs, container_name).items():
        if isinstance(value, dict):
            ref = value["secretKeyRef"]
            if ref.get("optional"):
                continue
            environment[name] = "experimently" if name == "POSTGRES_USER" else "f" * 64
        else:
            environment[name] = str(value)
    return environment


@pytest.mark.parametrize("container_name", ["api", "migrate"])
def test_chart_env_satisfies_the_production_settings_validators(
    docs, profile, container_name, tmp_path
):
    script = _MODULE_SETTINGS if profile == "full" else _CORE_SETTINGS
    if (
        profile == "full"
        and not (REPO_ROOT / "modules" / "backend" / "app" / "settings.py").is_file()
    ):
        pytest.skip(
            "core checkout: no modules package to build the full profile's settings"
        )
    environment = _environment_for_settings(docs, container_name)
    # A clean environment: the test process runs with TESTING=true, which
    # turns every hardening validator off, and the pod does not. The working
    # directory is empty so no local .env.prod can supply what the chart omits.
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PYTHONPATH": str(REPO_ROOT),
            **environment,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"the chart gives the {container_name} container {sorted(environment)}, and the "
        f"application refuses to start in production with it:\n{result.stderr[-3000:]}"
    )
