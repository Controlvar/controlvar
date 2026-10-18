# syntax=docker/dockerfile:1
# =========================================================================
# StreamPulse demo app (Next.js 14, pages router) — `demo` compose profile.
#
# Build context is the REPOSITORY ROOT: the app imports the React SDK from
# source (`sdk/react/src` via the alias in next.config.js), so both trees must
# be present at the same relative paths:
#
#   docker build -f demo/streampulse/Dockerfile \
#     --build-arg NEXT_PUBLIC_CONTROLVAR_API_KEY=<key> -t experimently-streampulse .
#
# NEXT_PUBLIC_* values are inlined into the bundle at build time (Next.js
# semantics), so the API key/URL the app uses are build args, not
# runtime env. docker-compose.yml passes the same STREAMPULSE_API_KEY it hands to
# the seed script.
# =========================================================================

ARG NODE_VERSION=22

# -------------------------------------------------------------------------
# Stage 1: build
# -------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS builder

ENV NEXT_TELEMETRY_DISABLED=1 \
    CI=true

WORKDIR /app

# React SDK source (consumed straight from src/, no build step needed).
COPY sdk/react/package.json sdk/react/tsconfig.json /app/sdk/react/
COPY sdk/react/src /app/sdk/react/src

# StreamPulse app
COPY demo/streampulse/package.json demo/streampulse/package-lock.json /app/demo/streampulse/
WORKDIR /app/demo/streampulse
RUN npm ci --no-audit --no-fund \
    # The SDK sources live outside the app tree; Node/TypeScript resolution
    # from /app/sdk/react/src walks up to /app/node_modules, so point that at
    # the app's modules (same React copy the webpack alias enforces).
    && ln -s /app/demo/streampulse/node_modules /app/node_modules
COPY demo/streampulse /app/demo/streampulse

# Each setting is taken from its build arg, else from the arg's name before
# the project was renamed, else the default here.
ARG NEXT_PUBLIC_CONTROLVAR_API_URL=""
ARG NEXT_PUBLIC_EXPERIMENTLY_API_URL=""
# The demo storefronts ship a NEXT_PUBLIC_ demo API key baked at build time:
# it is a public, seeded, demo-only key, which is what NEXT_PUBLIC_ means.
# hadolint ignore=DL3064
ARG NEXT_PUBLIC_CONTROLVAR_API_KEY=""
# hadolint ignore=DL3064
ARG NEXT_PUBLIC_EXPERIMENTLY_API_KEY=""
ARG NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL=""
ARG NEXT_PUBLIC_EXPERIMENTLY_DASHBOARD_URL=""
# hadolint ignore=DL3064
ENV NEXT_PUBLIC_CONTROLVAR_API_URL=${NEXT_PUBLIC_CONTROLVAR_API_URL:-${NEXT_PUBLIC_EXPERIMENTLY_API_URL:-http://localhost:8000}} \
    NEXT_PUBLIC_CONTROLVAR_API_KEY=${NEXT_PUBLIC_CONTROLVAR_API_KEY:-${NEXT_PUBLIC_EXPERIMENTLY_API_KEY}} \
    NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL=${NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL:-${NEXT_PUBLIC_EXPERIMENTLY_DASHBOARD_URL:-http://localhost:3000}}

RUN npm run build

# Production node_modules only (next, react, react-dom) for the runtime stage.
RUN npm ci --omit=dev --no-audit --no-fund

# -------------------------------------------------------------------------
# Stage 2: run (`next start`)
# -------------------------------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3300

RUN apk add --no-cache wget

WORKDIR /app/demo/streampulse

COPY --from=builder --chown=node:node /app/demo/streampulse/package.json ./package.json
COPY --from=builder --chown=node:node /app/demo/streampulse/next.config.js ./next.config.js
COPY --from=builder --chown=node:node /app/demo/streampulse/node_modules ./node_modules
COPY --from=builder --chown=node:node /app/demo/streampulse/.next ./.next

USER node

EXPOSE 3300

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3300/ >/dev/null 2>&1 || exit 1

CMD ["npx", "next", "start", "-p", "3300"]
