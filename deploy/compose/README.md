# Self-hosting with Docker Compose

`compose.yml` in this directory runs Controlvar on one Docker host from the
published images. It builds nothing, starts no demo applications and creates
no demo users. For a development stack built from a checkout, use the
repository's `docker-compose.yml` and the
[Docker guide](https://docs.controlvar.com/getting-started/docker-guide/)
instead.

## Run it

From this directory:

```bash
./generate-env.sh
docker compose up -d --wait
docker compose logs api
```

`generate-env.sh` writes `.env` beside `compose.yml` with a generated
`SECRET_KEY`, `POSTGRES_PASSWORD`, `FIRST_SUPERUSER_PASSWORD` and
`AUDIT_HMAC_KEY`, readable by your user only. Give it the URL people will open
and the first administrator's email:

```bash
PUBLIC_BASE_URL=https://controlvar.example.com \
FIRST_SUPERUSER=you@example.com ./generate-env.sh
```

Without them it writes `http://localhost:8080` and `admin@example.com`.
`EXPERIMENTLY_PROFILE=full` makes it write the full profile, and `ENV_FILE`
writes the file somewhere else.
It needs `openssl`, or `python3` when `openssl` is absent, and with neither it
stops without writing anything.

It refuses to overwrite an existing `.env`, whose secrets the running install
already uses. To start again from nothing, run `docker compose down -v`, which
deletes the database, and delete `.env` first.

`docker compose up -d --wait` starts PostgreSQL, Redis, the API and the
dashboard, and returns once they are healthy. The first start creates the
database schema and the first administrator: sign in at `PUBLIC_BASE_URL` as
`FIRST_SUPERUSER` with the `FIRST_SUPERUSER_PASSWORD` from `.env`. That
password is read only while the database has no users: changing it later does
nothing.

On a Mac with Apple Silicon and Docker Desktop, the API and the dashboard run
under amd64 emulation, because their images are published for `linux/amd64`
only, and PostgreSQL and Redis run natively (measured on an Apple M4 with the
v0.34.3 images).

Only the dashboard publishes a port, on `127.0.0.1:8080` by default, and it
passes `/api` to the API. Put your TLS reverse proxy in front of it and pass
the Host header unchanged: the API answers only for the host name of
`PUBLIC_BASE_URL`.

## Settings

`.env` holds them, and `.env.example` lists every setting `compose.yml` reads.
Compose refuses to start while a required one is unset or empty, and names it.

| Setting | Meaning |
|---|---|
| `PUBLIC_BASE_URL` | required: the URL people open |
| `SECRET_KEY` | required |
| `FIRST_SUPERUSER` | required: the first administrator's email |
| `FIRST_SUPERUSER_PASSWORD` | required |
| `POSTGRES_PASSWORD` | required |
| `EXPERIMENTLY_PROFILE` | `core` (the default) or `full` |
| `AUDIT_HMAC_KEY` | needed by the `full` profile |
| `HTTP_BIND`, `HTTP_PORT` | where the dashboard listens: `127.0.0.1` and `8080` by default |
| `POSTGRES_SERVER` | an external PostgreSQL instead of the bundled one; then start only `docker compose up -d api web` |

Run an external PostgreSQL server with its `TimeZone` set to UTC.

The image lines name the images of this file's own release,
`ghcr.io/controlvar/controlvar:<profile>-X.Y.Z` and
`ghcr.io/controlvar/controlvar-web:<profile>-X.Y.Z`.
[Releases, images and verification](https://docs.controlvar.com/self-hosting/releases/)
shows how to check what you pulled.

## What CI runs

The `compose-production` workflow runs this file on every pull request that
changes it, with that change's images under the published names. It runs
`generate-env.sh` twice, and the second run must refuse and leave `.env` as it
was. It starts the stack with `docker compose up -d --wait` and signs in as
the first administrator through the dashboard's port. It checks that a request
for another host name answers `400`, that the demo login answers `401`, and
that PostgreSQL, Redis and the API publish no port.
