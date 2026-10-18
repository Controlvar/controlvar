# syntax=docker/dockerfile:1
# =========================================================================
# Controlvar API image.
#
# Build context is the REPOSITORY ROOT (the code imports `backend.app.*`):
#
#   docker build -f backend/Dockerfile -t experimently-api:core .
#   docker build -f backend/Dockerfile --target full -t experimently-api:full .
#
# Targets:
#   core  (default) the core profile — backend/ only
#   full  the full profile — backend/ plus modules/ and the module-only
#         dependencies (modules/requirements.lock). Needs a full checkout: on
#         a core one, which has no modules/ directory, it produces an image
#         with an empty /app/modules that refuses to start outside development.
#
# Runtime contract:
#   ENTRYPOINT backend/docker-entrypoint.sh  (waits for Postgres, bootstraps the
#              schema when RUN_MIGRATIONS=true, applies SEED=... once)
#   CMD        uvicorn backend.app.main:app --workers ${WEB_CONCURRENCY:-1}
#   HEALTHCHECK GET /health/live
# =========================================================================

ARG PYTHON_VERSION=3.11

# -------------------------------------------------------------------------
# Stage 1: builder — resolve and install the runtime dependency set into a venv
# -------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# gcc + libc headers: a few pins (psutil on arm64) have no binary wheel and
# build from source; libpq-dev for psycopg2 headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        linux-libc-dev \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# The lock is the fully resolved, hashed closure of runtime.txt (`make lock`,
# uv pip compile --universal --generate-hashes). Installing it with
# --require-hashes makes the image reproducible and refuses any artefact that
# does not match a recorded hash; `make lock-check` / the lint job fail when
# runtime.txt is bumped without regenerating it.
COPY backend/requirements/runtime.lock /tmp/runtime.lock
RUN pip install --upgrade pip && pip install --require-hashes -r /tmp/runtime.lock

# The venv's start-up hook (#1265): when backend/docker-entrypoint.sh found that
# cryptography's OpenSSL crashes on this CPU, it sets OPENSSL_armcap=0 in every
# Python process of the venv, including the ones `docker compose exec` starts.
# A no-op everywhere else. In the venv, so both profiles get it through their
# `COPY --from` of /opt/venv; each of them asserts below that it is loaded.
ARG PYTHON_VERSION
COPY backend/docker/sitecustomize.py /opt/venv/lib/python${PYTHON_VERSION}/site-packages/sitecustomize.py

# -------------------------------------------------------------------------
# Stage 2: runtime — everything the two profiles share EXCEPT the venv.
#
# The venv is copied in by the profile stages below, not here: the full profile
# needs a bigger one, and a `COPY --from` on top of an inherited /opt/venv
# would leave both copies in the image.
# -------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG BUILD_DATE
ARG GIT_COMMIT
# The release tag, without its leading "v". Passed by .github/workflows/
# release.yml, which has already run scripts/check_version_sources.py --expect
# against the tag, so the label and the /app/VERSION copied in below cannot
# disagree. A plain `docker build` that passes nothing labels the image
# "0.0.0+unknown" -- an unreleasable version by construction (the same string
# backend/app/core/version.py falls back to), rather than a stale number that
# reads like a release.
ARG VERSION=0.0.0+unknown

LABEL org.opencontainers.image.title="Controlvar API" \
      org.opencontainers.image.description="FastAPI backend for the Controlvar experimentation platform" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/Controlvar/controlvar" \
      org.opencontainers.image.url="https://github.com/Controlvar/controlvar"

# The profile is NOT a build argument: it is the target you build. `core` and
# `full` below each set EXPERIMENTLY_PROFILE and the profile label for
# themselves, and this stage is never a build target of its own. It carried an
# `ARG PROFILE=core` that nothing ever passed and both children overrode.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    WEB_CONCURRENCY=1 \
    PORT=8000

# libpq5: psycopg2 runtime; postgresql-client: pg_isready for the entrypoint;
# curl: HEALTHCHECK.
#
# `apt-get upgrade` first: the python:*-slim base is rebuilt on its own
# schedule, and between a Debian security release and that rebuild the base
# ships the old package. That happened with OpenSSL 3.5.7-1~deb13u2 (fixed in
# deb13u3, CVE-2026-75804 and CVE-2026-84782): the security-scan job's Trivy
# step fails on every fixable HIGH, so every pull request went red until the
# runtime image took the update itself. Upgrading here takes whatever the
# Debian archive has at build time rather than one named package, so the next
# lag is covered too. Only this stage ships; `builder` and `modules-src` are
# discarded.
#
# OS_PACKAGES_REFRESHED: the workflow steps that build this image pass the UTC
# date (backend/tests/unit/infrastructure/test_runtime_image_os_updates.py
# holds every one to it). A build cache keeps this layer while nothing it
# depends on changes, and the Debian archive is not one of those things, so a
# layer cached before an update kept shipping the old package. With the date,
# a cached copy is reused only by builds that pass the same date. A plain
# `docker build` gets the default, and its own cache decides as before.
ARG OS_PACKAGES_REFRESHED=unset
RUN printf 'OS packages refreshed: %s\n' "${OS_PACKAGES_REFRESHED}" \
    && apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser \
    && rm -rf /usr/local/lib/python*/site-packages/setuptools \
              /usr/local/lib/python*/site-packages/setuptools-* \
              /usr/local/lib/python*/site-packages/pkg_resources \
              /usr/local/lib/python*/site-packages/wheel \
              /usr/local/lib/python*/site-packages/wheel-* \
              /usr/local/lib/python*/site-packages/pip \
              /usr/local/lib/python*/site-packages/pip-* \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
              /usr/local/bin/wheel

# The base image's own packaging tools, removed just above, and the venv's,
# removed in the two profile stages below. Neither is used at runtime: PATH
# puts /opt/venv first, the entrypoint runs uvicorn and alembic, and nothing
# under backend/app or modules/backend/app imports pip, setuptools or
# pkg_resources. They are, however, most of what a vulnerability scanner finds
# in this image -- setuptools and its vendored jaraco.context/wheel, and pip's
# vendored msgpack. Measured on the core image: 6 HIGH/CRITICAL findings with a
# fix available before, 0 after. (The other 55 are unfixed Debian CVEs in the
# python:3.11-slim base, which is why the scan runs --ignore-unfixed.)
#
# A runtime image that cannot install packages is also the point: `pip install`
# inside a running container is not a supported operation here.

WORKDIR /app

# Application code. The layout under /app mirrors the repository so that
# `backend.app.main:app`, `python -m backend.app.db.bootstrap` and the seed
# scripts resolve exactly as they do from a checkout.
COPY --chown=appuser:appgroup backend/ /app/backend/
# The version the running API reports. backend/app/core/version.py walks three
# directories up from itself, which is /app here exactly as it is the
# repository root in a checkout -- so the image and a developer's tree resolve
# the version by the same path, with no image-only branch to get wrong. It is
# the file, not the ARG above, because the label is metadata an operator reads
# and this is what /health and /api/v1/modules answer.
COPY --chown=appuser:appgroup VERSION /app/VERSION
# The requirements files have no runtime use -- they were inputs to the builder
# stage, which took them from the build context, not from here. Leaving them in
# the image is not merely untidy: trivy's pip analyser reads any file named
# requirements.txt, image scans included, so `/app/backend/requirements.txt`
# made the *development* closure (pytest, moto, locust, aws-cdk-lib) part of
# what the image gate audits. That gate cannot express the exceptions
# security/dependency-audit.toml accepts, so the two would have contradicted
# each other about the same pin the moment one of those advisories gained a
# fix. .dockerignore cannot exclude them -- the builder needs
# backend/requirements/runtime.lock from the same context -- so they go here.
#
# A pattern, not three named paths: the first attempt removed requirements.txt
# and requirements/ and left backend/requirements-monitoring.txt sitting there
# -- exactly the kind of file trivy reads. That file has since been deleted
# (#188), which is the point: a named list misses whatever nobody remembered,
# and would now name something gone. The assertion below is what makes either
# case loud.
RUN find /app/backend -maxdepth 2 -name 'requirements*' -exec rm -rf {} + \
    && chmod +x /app/backend/docker-entrypoint.sh \
    && mkdir -p /data/demo \
    && chown -R appuser:appgroup /data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://localhost:${PORT:-8000}/health/live || exit 1

ENTRYPOINT ["/app/backend/docker-entrypoint.sh"]
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# -------------------------------------------------------------------------
# Stage 3a: modules-src — the modules/ tree, or an empty stand-in for it.
#
# Every reference to modules/ in this file goes through this one stage, because
# a COPY that names modules/ by an explicit path cannot be built from a *core*
# checkout, which has no modules/ at all. BuildKit builds only the stages the
# requested --target needs, so it never looks; a classic builder
# (DOCKER_BUILDKIT=0, Compose v1, Kaniko, older buildah) has no such notion and
# runs every stage in file order, and `core` is the LAST stage here — so a
# plain `docker build -f backend/Dockerfile .` on a core checkout reaches these
# module stages and dies in them with "COPY failed: file not found in build
# context: modules/requirements.lock".
#
# A glob alone does not fix it either: both builders reject a pattern that
# matches nothing with "COPY failed: no source files were specified". So each
# glob is paired with runtime.lock, which is always in the context, and the
# companion is deleted again below. What is left is the same layout either way
# — /modules/__init__.py, /modules/requirements.lock (empty when the context
# had none) and /modules/backend/ — which the two stages below then take by
# explicit path.
#
# `modules*/...` and not the bare `modules*`: the latter also matches the
# repository's modules-manifest.txt.
#
# A `--target full` build from a core checkout is therefore no longer refused
# at build time; it produces an image whose /app/modules is empty, and
# `abort_if_modules_broken()` refuses to start it outside development.
# -------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS modules-src

COPY backend/requirements/runtime.lock modules*/__init__.py modules*/requirements.lock /modules/
COPY backend/requirements/runtime.lock modules*/backend /modules/backend/
RUN rm -f /modules/runtime.lock /modules/backend/runtime.lock \
    && touch /modules/__init__.py /modules/requirements.lock

# -------------------------------------------------------------------------
# Stage 3b: builder-full — the builder venv plus the module-only dependencies.
#
# The modules import packages the core profile never does (python3-saml/xmlsec,
# authlib and their closure). A `full` image without them still boots and
# serves every route — and then refuses the SAML flow. They must be in the
# image, and only in the `full` one.
#
# modules/requirements.lock is the hashed closure of modules/requirements.txt
# (`make lock`), resolved under backend/requirements.txt as a constraint so the
# two locks never disagree about a shared package; `make lock-check` and the
# lint job fail when they drift. Installed here rather than in the
# `full` stage below because this stage has the toolchain the sdist-only pins
# need (thrift 0.20.0 publishes no wheel at all), and because the install then
# caches independently of the module sources.
# -------------------------------------------------------------------------
FROM builder AS builder-full

COPY --from=modules-src /modules/requirements.lock /tmp/modules.lock
# Empty means a core checkout (see modules-src); there is nothing to add.
RUN if [ -s /tmp/modules.lock ]; then \
        pip install --require-hashes -r /tmp/modules.lock; \
    fi

# -------------------------------------------------------------------------
# Stage 3c: full — runtime plus the modules package (`--target full`).
# -------------------------------------------------------------------------
FROM runtime AS full

ENV EXPERIMENTLY_PROFILE=full
LABEL io.experimently.profile="full"

# The core closure *plus* the module dependencies.
COPY --from=builder-full /opt/venv /opt/venv
# See the note in the runtime stage. Done here and not in `builder`, which
# `builder-full` extends and which therefore still needs pip. USER root for the
# duration: the runtime stage has already dropped to appuser, and rm on a
# root-owned tree fails -- loudly, which is how this was caught, but a silent
# `|| true` would have left the toolchain in place and the scan gate red with
# no explanation.
USER root
RUN rm -rf /opt/venv/lib/python*/site-packages/pip \
           /opt/venv/lib/python*/site-packages/pip-* \
           /opt/venv/lib/python*/site-packages/setuptools \
           /opt/venv/lib/python*/site-packages/setuptools-* \
           /opt/venv/lib/python*/site-packages/pkg_resources \
           /opt/venv/lib/python*/site-packages/wheel \
           /opt/venv/lib/python*/site-packages/wheel-* \
           /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.* \
           /opt/venv/lib/python*/site-packages/_distutils_hack \
           /opt/venv/lib/python*/site-packages/distutils-precedence.pth \
           /usr/local/lib/python*/site-packages/_distutils_hack \
           /usr/local/lib/python*/site-packages/distutils-precedence.pth
# ...and then prove it. `rm -rf` on a path that does not exist exits 0, so
# every line above fails OPEN: a base-image layout change, or a
# PYTHON_VERSION the paths no longer match, would leave the toolchain in place
# and nothing would say so. The only other thing that would notice is the
# trivy gate, and only while a *fixable* advisory happens to exist in one of
# those packages -- the moment there is none, a green scan would mean nothing.
#
# `find` rather than `python -c "import pip"`: import failure is also what a
# broken install looks like, and this must distinguish "absent" from "broken".
RUN set -eu; \
    for name in pip setuptools wheel pkg_resources _distutils_hack; do \
        found="$(find /opt/venv /usr/local/lib -maxdepth 4 -name "$name" -print -quit 2>/dev/null || true)"; \
        if [ -n "$found" ]; then \
            echo "the packaging toolchain survived removal: $found" >&2; \
            exit 1; \
        fi; \
    done; \
    if command -v pip >/dev/null 2>&1; then \
        echo "pip is still on PATH: $(command -v pip)" >&2; \
        exit 1; \
    fi; \
    echo "packaging toolchain removed from the venv and the base image"
USER appuser

# The venv's start-up hook is where the builder stage put it and Python loads
# it at start-up -- not merely importable: a path that misses site-packages
# (a PYTHON_VERSION the COPY above no longer matches) would leave nothing to
# load, and every `docker compose exec` on an affected CPU would crash again.
RUN python -c 'import sys; m = sys.modules.get("sitecustomize"); assert m and m.__file__.startswith("/opt/venv/"), m; print("venv start-up hook loaded from", m.__file__)'

# modules/backend, not modules/ whole: modules/frontend is the dashboard's
# module code, which frontend/Dockerfile needs in the build context and this
# image has no use for. modules/backend/tests, modules/infrastructure and
# modules/lambda are kept out of the context by .dockerignore, exactly as their
# backend/ counterparts are.
COPY --from=modules-src --chown=appuser:appgroup /modules/__init__.py /app/modules/
COPY --from=modules-src --chown=appuser:appgroup /modules/backend /app/modules/backend/

# What must not be in a published image, asserted at the END of the profile
# stage -- after `full` has copied modules/, which the earlier placement in the
# `runtime` stage ran before. Covers the .dockerignore exclusions too: an
# exclusion nothing checks fails open the next time that file is reordered.
#
# The agent instruction files are matched by a glob rather than by name.
# There are several of them -- the repository root, backend/, and whatever a
# developer keeps locally -- so naming one asserted about one. A literal name
# would also pin one spelling of a file whose set changes as agents come and
# go, leaving this line asserting about something that need not exist.
RUN set -eu; \
    for pattern in 'requirements*' 'CLAUDE*' 'Dockerfile' '*.pyc'; do \
        found="$(find /app -name "$pattern" -print -quit 2>/dev/null || true)"; \
        if [ -n "$found" ]; then \
            echo "the image ships $found, which it must not" >&2; \
            exit 1; \
        fi; \
    done; \
    echo "no requirements files, internal docs or build files in the image"

# -------------------------------------------------------------------------
# Stage 4: core — the core image, with the core closure only: none of
# modules/requirements.lock and no modules/ directory. Last stage = default
# target, so a plain `docker build -f backend/Dockerfile .` builds this one.
# -------------------------------------------------------------------------
FROM runtime AS core
ENV EXPERIMENTLY_PROFILE=core
LABEL io.experimently.profile="core"

COPY --from=builder /opt/venv /opt/venv
# See the note in the runtime stage. Done here and not in `builder`, which
# `builder-full` extends and which therefore still needs pip. USER root for the
# duration: the runtime stage has already dropped to appuser, and rm on a
# root-owned tree fails -- loudly, which is how this was caught, but a silent
# `|| true` would have left the toolchain in place and the scan gate red with
# no explanation.
USER root
RUN rm -rf /opt/venv/lib/python*/site-packages/pip \
           /opt/venv/lib/python*/site-packages/pip-* \
           /opt/venv/lib/python*/site-packages/setuptools \
           /opt/venv/lib/python*/site-packages/setuptools-* \
           /opt/venv/lib/python*/site-packages/pkg_resources \
           /opt/venv/lib/python*/site-packages/wheel \
           /opt/venv/lib/python*/site-packages/wheel-* \
           /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.* \
           /opt/venv/lib/python*/site-packages/_distutils_hack \
           /opt/venv/lib/python*/site-packages/distutils-precedence.pth \
           /usr/local/lib/python*/site-packages/_distutils_hack \
           /usr/local/lib/python*/site-packages/distutils-precedence.pth
# ...and then prove it. `rm -rf` on a path that does not exist exits 0, so
# every line above fails OPEN: a base-image layout change, or a
# PYTHON_VERSION the paths no longer match, would leave the toolchain in place
# and nothing would say so. The only other thing that would notice is the
# trivy gate, and only while a *fixable* advisory happens to exist in one of
# those packages -- the moment there is none, a green scan would mean nothing.
#
# `find` rather than `python -c "import pip"`: import failure is also what a
# broken install looks like, and this must distinguish "absent" from "broken".
RUN set -eu; \
    for name in pip setuptools wheel pkg_resources _distutils_hack; do \
        found="$(find /opt/venv /usr/local/lib -maxdepth 4 -name "$name" -print -quit 2>/dev/null || true)"; \
        if [ -n "$found" ]; then \
            echo "the packaging toolchain survived removal: $found" >&2; \
            exit 1; \
        fi; \
    done; \
    if command -v pip >/dev/null 2>&1; then \
        echo "pip is still on PATH: $(command -v pip)" >&2; \
        exit 1; \
    fi; \
    echo "packaging toolchain removed from the venv and the base image"
USER appuser

# The venv's start-up hook is where the builder stage put it and Python loads
# it at start-up -- not merely importable: a path that misses site-packages
# (a PYTHON_VERSION the COPY above no longer matches) would leave nothing to
# load, and every `docker compose exec` on an affected CPU would crash again.
RUN python -c 'import sys; m = sys.modules.get("sitecustomize"); assert m and m.__file__.startswith("/opt/venv/"), m; print("venv start-up hook loaded from", m.__file__)'

# What must not be in a published image, asserted at the END of the profile
# stage -- after `full` has copied modules/, which the earlier placement in the
# `runtime` stage ran before. Covers the .dockerignore exclusions too: an
# exclusion nothing checks fails open the next time that file is reordered.
#
# The agent instruction files are matched by a glob rather than by name.
# There are several of them -- the repository root, backend/, and whatever a
# developer keeps locally -- so naming one asserted about one. A literal name
# would also pin one spelling of a file whose set changes as agents come and
# go, leaving this line asserting about something that need not exist.
RUN set -eu; \
    for pattern in 'requirements*' 'CLAUDE*' 'Dockerfile' '*.pyc'; do \
        found="$(find /app -name "$pattern" -print -quit 2>/dev/null || true)"; \
        if [ -n "$found" ]; then \
            echo "the image ships $found, which it must not" >&2; \
            exit 1; \
        fi; \
    done; \
    echo "no requirements files, internal docs or build files in the image"
