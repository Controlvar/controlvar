# Upgrading to Controlvar

The project called Experimently is now Controlvar. Read this before your next
upgrade.

## Images

From 0.35.0 on, the images are `ghcr.io/controlvar/controlvar` and
`ghcr.io/controlvar/controlvar-web`, with the same tags as before. Change the
image names in your own compose file, manifests or values. Their signatures name
the new repository: [Releases, images and verification](releases.md) gives the
identity for each version.

## Docker Compose

Replace `deploy/compose/compose.yml` with the file of the new release, then run
`docker compose pull` and `docker compose up -d`. The file keeps the project name
`experimently-selfhost` and the database user `experimently` on purpose: the
volumes are named after the project, so your database is the one you had. Do not
change either value.

## Helm

The chart's name is now `controlvar`; it is still in `charts/experimently` in the
repository. Upgrade with the release name you installed with, as
[Kubernetes (Helm)](kubernetes.md) shows. Every object keeps its name, the
PostgreSQL StatefulSet and its volume included.

## Settings and integrations

The `EXPERIMENTLY_PROFILE` setting keeps its name. Jira and Salesforce senders
still authenticate with the `X-Experimently-Webhook-Secret` header, so nothing
changes in their configuration.

The MCP server's manifest, `GET /api/v1/mcp/manifest`, now gives the name
`controlvar`. A client that matched the former name on it needs the new one.

The dashboard keeps its sign-in token under the same key and the API keeps the
issuer it signs tokens with, so nobody is signed out by the upgrade.

## The development stack

The root `docker-compose.yml` takes its project name from the directory of the
checkout. A new clone in a directory named `controlvar` starts with new, empty
volumes; a checkout in a directory still named `experimently` keeps its data.
