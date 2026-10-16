# syntax=docker/dockerfile:1
# =========================================================================
# Controlvar dashboard image.
#
#   docker build -f frontend/Dockerfile -t experimently-web:core .
#   docker build -f frontend/Dockerfile -t experimently-web:full \
#       --build-arg EXPERIMENTLY_PROFILE=full .
#
# The build context is the REPOSITORY ROOT (like backend/Dockerfile): the
# modules' dashboard code lives in modules/frontend/, next to frontend/, not
# inside it. (The documentation is no longer rendered in-app, so docs/ is not
# copied. The dashboard's docs links go to the Markdown under docs/ on GitHub,
# unless NEXT_PUBLIC_DOCS_URL below names a built MkDocs site.)
#
# EXPERIMENTLY_PROFILE selects what goes in:
#   core (default)  never reads modules/frontend/ — a core checkout, which has
#                   no modules/ at all, builds unchanged — and the `@modules/*`
#                   alias resolves to the stub tree (frontend/modules-alias.js).
#   full            copies modules/frontend/ into the image and builds the
#                   full-profile dashboard; the build fails if the tree is
#                   missing.
# core is the default for the same reason backend/Dockerfile's last (and so
# default) target is `core`: a plain `docker build` of either image must
# produce a matching pair, and must work on a checkout with no modules/ in it.
# The choice is made with a stage name, because COPY has no conditional: under
# BuildKit only the stage the profile names is built, so the missing directory
# is never looked at, and under a classic builder -- which builds every stage
# -- the glob in stage 0 keeps the look harmless. The build asserts the
# resulting bundle matches the profile.
#
# Stage 1 builds the Next.js static export (output: 'export' -> out/) and
# generates the nginx location blocks for the dynamic routes from
# .next/routes-manifest.json. Stage 2 serves it with nginx-unprivileged on
# port 8080 and proxies /api/, /ws/ and /health* to API_UPSTREAM (default
# http://api:8000), so the browser talks to a single origin and the API base
# baked into the bundle can stay empty (NEXT_PUBLIC_API_URL="").
# =========================================================================

ARG NODE_VERSION=22
ARG EXPERIMENTLY_PROFILE=core

# -------------------------------------------------------------------------
# Stage 0: the modules tree, or an empty directory, by profile
#
# `modules*/frontend`, not `modules/frontend`: a COPY that names modules/ by an
# explicit path cannot be built from a *core* checkout, which has no modules/
# at all. BuildKit builds only the stages the selected profile needs, so it
# never looks at this one on a core build -- but a classic builder
# (DOCKER_BUILDKIT=0, Compose v1, Kaniko, older buildah) has no such notion and
# runs every stage in file order, so a plain `docker build -f
# frontend/Dockerfile .` on a core checkout died here with "COPY failed: file
# not found in build context: modules/frontend". backend/Dockerfile's
# modules-src stage carries the same fix for the same reason.
#
# A glob alone does not fix it either: both builders reject a pattern that
# matches nothing with "COPY failed: no source files were specified". So the
# glob is paired with frontend/tsconfig.core.json, which is always in the
# context, and the companion is deleted again -- leaving exactly what the
# explicit COPY left when modules/ is there, and an empty directory when it is
# not. (Any always-present file would do; that one is picked because
# modules/frontend/ can never hold a file of the same name to lose.)
# `--build-arg EXPERIMENTLY_PROFILE=full` on a core checkout is then refused a
# few lines below, by the builder's own `test -d /app/modules/frontend/src`,
# which says what is missing.
# -------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS modules-tree-full
COPY frontend/tsconfig.core.json modules*/frontend /modules/frontend/
RUN rm -f /modules/frontend/tsconfig.core.json

FROM node:${NODE_VERSION}-alpine AS modules-tree-core
RUN mkdir -p /modules/frontend

# DL3006 wants a tag: this is a stage reference, not an image, and hadolint
# cannot see that through the variable.
# hadolint ignore=DL3006
FROM modules-tree-${EXPERIMENTLY_PROFILE} AS modules-tree

# -------------------------------------------------------------------------
# Stage 1: build
# -------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS builder

ARG EXPERIMENTLY_PROFILE
WORKDIR /app/frontend

# EXPERIMENTLY_PROFILE=core also forces the core resolution of `@modules/*`
# inside the build, so the profile holds even if a tree slipped in.
ENV NEXT_TELEMETRY_DISABLED=1 \
    CI=true \
    EXPERIMENTLY_PROFILE=${EXPERIMENTLY_PROFILE}

# Full install: next build needs typescript, tailwindcss, postcss,
# autoprefixer, all of which are devDependencies.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# modules-alias.js looks for ../modules/frontend/src relative to the frontend dir.
COPY --from=modules-tree /modules/frontend/ /app/modules/frontend/
# Relative API base by default: nginx proxies /api/ to the backend. Pass an
# absolute URL only when the dashboard is served from a different origin.
ARG NEXT_PUBLIC_API_URL=""
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
# Where the dashboard's docs links go (frontend/src/services/docs.ts). Empty,
# the default here, means the Markdown under docs/ on GitHub's main branch. Set
# it to the origin of a built MkDocs site (the published one, or one this
# deployment hosts) to link there instead; only `npm run build:marketing` sets it.
ARG NEXT_PUBLIC_DOCS_URL=""
ENV NEXT_PUBLIC_DOCS_URL=${NEXT_PUBLIC_DOCS_URL}

RUN npm run build \
    && test -f .next/routes-manifest.json \
    && node scripts/nginx-routes.mjs --out /app/nginx/routes.conf \
    && test -d out \
    # The bundle must match the profile: a core image carries no chunk that
    # names a module route, a full image carries them.
    # The positive half matters as much as the negative one -- without it the
    # grep also passes when the page vanished entirely. It is a string, not a
    # chunk filename: Turbopack (the default bundler from Next 16) emits flat
    # content-hashed chunks with no `chunks/pages/` directory, so the old
    # `find out/_next/static/chunks/pages -name 'workspaces-*.js'` matched
    # nothing in either profile. `Separate teams into workspaces` is the stub
    # page's own description (frontend/src/modules-stub/pages/workspaces/index.tsx);
    # the real module's page says "Isolated project namespaces" instead, so the
    # string is in the core bundle and in no full one.
    && if [ "$EXPERIMENTLY_PROFILE" = "core" ]; then \
         ! grep -rlE '/api/v1/workspaces|/api/v1/rbac/' out/_next/static >/dev/null \
         && grep -rl 'Separate teams into workspaces' out/_next/static >/dev/null; \
       else \
         test -d /app/modules/frontend/src \
         && grep -rlE '/api/v1/rbac/' out/_next/static >/dev/null; \
       fi

# -------------------------------------------------------------------------
# Stage 2: serve
# -------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.31-alpine AS runtime

ARG BUILD_DATE
ARG GIT_COMMIT
ARG EXPERIMENTLY_PROFILE
# The release tag without its leading "v"; see the same ARG in
# backend/Dockerfile. The default is the unreleasable placeholder, so an image
# built by hand never claims a version it does not have.
ARG VERSION=0.0.0+unknown
# source and url are set explicitly because the base image sets both to its own
# repository, and a LABEL is inherited unless overridden: without these two
# lines the dashboard image names nginx's repository as where it came from.
LABEL org.opencontainers.image.title="Controlvar Dashboard" \
      org.opencontainers.image.description="Static Next.js dashboard served by nginx, proxying /api to the Controlvar API" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/Controlvar/controlvar" \
      org.opencontainers.image.url="https://github.com/Controlvar/controlvar" \
      io.experimently.profile="${EXPERIMENTLY_PROFILE}"

ENV API_UPSTREAM=http://api:8000

USER root
# Take Alpine's security fixes at build time: the base image is rebuilt on its
# own schedule, and Trivy fails the scan on any HIGH with a fix available
# (pcre2 10.48-r0 -> 10.49-r0 was the first). The API image does the same with
# apt-get upgrade. OS_PACKAGES_REFRESHED is the UTC date the workflows pass, so
# a cached copy of this layer is reused only by builds that pass the same date;
# see the same ARG in backend/Dockerfile.
ARG OS_PACKAGES_REFRESHED=unset
RUN printf 'OS packages refreshed: %s\n' "${OS_PACKAGES_REFRESHED}" \
    && apk upgrade --no-cache
# The unprivileged image ships a default server on 8080; ours replaces it via
# the template rendered by docker-entrypoint.sh.
RUN rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /etc/nginx/templates \
    && chown -R nginx:nginx /etc/nginx/conf.d /etc/nginx/templates
COPY --chown=nginx:nginx frontend/nginx.conf /etc/nginx/templates/default.conf.template
COPY --chown=nginx:nginx frontend/docker-entrypoint.sh /usr/local/bin/controlvar-entrypoint.sh
COPY --from=builder --chown=nginx:nginx /app/nginx/routes.conf /etc/nginx/controlvar-routes.conf
COPY --from=builder --chown=nginx:nginx /app/frontend/out /usr/share/nginx/html
RUN chmod +x /usr/local/bin/controlvar-entrypoint.sh
USER nginx

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/controlvar-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
