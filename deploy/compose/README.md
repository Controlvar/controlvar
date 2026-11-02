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
passes `/api` to the API. Open it at your `PUBLIC_BASE_URL`,
`http://localhost:8080` by default (with your `HTTP_PORT`, if you changed it),
not at `http://127.0.0.1:8080`: the API refuses the sign-in there with `400`.
Put your TLS reverse proxy in front of it and pass the Host header unchanged:
the API answers only for the host name of `PUBLIC_BASE_URL`.

Point an SDK's base URL at the dashboard's address (`http://localhost:8080` by
default), because this stack does not publish the API on port 8000. The
`localhost:8000` on the SDK pages is the
[Quick Start](https://docs.controlvar.com/getting-started/quick-start/)'s
address.

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
| `FORWARDED_ALLOW_IPS` | a TLS proxy on another machine whose `X-Forwarded-For` the API should believe; empty for a proxy on this host that connects to `127.0.0.1` (see [Users' addresses](#users-addresses)) |
| `POSTGRES_SERVER` | an external PostgreSQL instead of the bundled one; then start only `docker compose up -d api web` |
| `WAREHOUSE_CREDENTIALS_KEYS` | turns on warehouse analysis in the `full` profile: Fernet keys, comma-separated, newest first, that encrypt the warehouse credentials the API stores; while it is empty, storing or using a credential answers `503 credentials_unavailable` |
| `ANTHROPIC_API_KEY` | turns on Anthropic variants in LLM experiments and also Claude calls in `POST /api/v1/ai/design` and `POST /api/v1/ai/interpret/{experiment_id}` (limited to 10 requests a minute); without it those two routes answer from built-in templates |
| `OPENAI_API_KEY` | turns on OpenAI variants in LLM experiments |
| `GEMINI_API_KEY` | turns on Gemini (`google`) variants in LLM experiments |

Run an external PostgreSQL server with its `TimeZone` set to UTC.

The last four settings are optional. A key set to an empty value is the same
as a key that is not set, and both compose files pass these keys empty by
default. Put each key in the `.env` file next to the compose file: a key
exported in another terminal does not reach the container, and one exported in
the terminal that runs `docker compose up` does.

The image lines name the images of this file's own release,
`ghcr.io/controlvar/controlvar:<profile>-X.Y.Z` and
`ghcr.io/controlvar/controlvar-web:<profile>-X.Y.Z`.
[Releases, images and verification](https://docs.controlvar.com/self-hosting/releases/)
shows how to check what you pulled.

## Users' addresses

The API counts its rate limits per user address and writes that address on
its access lines. Behind your TLS reverse proxy it reads the address from the
`X-Forwarded-For` header, so set your proxy to send it; in nginx that is
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`.

The API believes that header from the dashboard container and from a TLS
proxy on this host that connects to `127.0.0.1`, as above, and from the
proxies in `FORWARDED_ALLOW_IPS`: IP addresses and networks, separated by
commas. Leave it empty for a proxy on this host that connects to `127.0.0.1`.
For a proxy on another machine, put its address in `.env`, such as
`FORWARDED_ALLOW_IPS=192.0.2.10` for a proxy at `192.0.2.10`, and run
`docker compose up -d`. A proxy in a container on a Docker network of its own
connects from that network: give the network's subnet, which
`docker network inspect` shows. Behind a TLS proxy, keep `HTTP_BIND` at
`127.0.0.1`, so that the proxy is the only way in.

Check it once people have used the dashboard:

```bash
docker compose logs api 2>&1 | grep '"uvicorn.access"' | grep ' /api/' \
  | sed -E 's/.*"event": "([^ ]*):[0-9]+ - .*/\1/' | sort | uniq -c | sort -rn | head
```

Each line is an address and the number of API requests from it. Your users'
addresses mean it works. If every request comes from one private address,
such as `172.18.0.1`, the API sees a proxy instead of your users: either your
TLS proxy sends no `X-Forwarded-For`, or it is on another machine (or connects
to this host's network address rather than `127.0.0.1`) and
`FORWARDED_ALLOW_IPS` does not list it. `docker compose logs web` ends each
line with the `X-Forwarded-For` the dashboard received, `"-"` when there was
none, and `docker compose logs api 2>&1 | grep 'trusted proxies'` shows what
the API believes.

## Back up and restore

A backup is two files, taken together: a dump of the database and your `.env`.
Keep both outside the Docker volumes, on another machine if you can, and keep
them private: `.env` holds the secrets.

Every command below names the Compose project, `experimently-selfhost`, which
is the `name:` in `compose.yml`, and none of them deletes a volume.

### Back up

From this directory:

```bash
mkdir -m 700 -p ~/controlvar-backups
docker compose -p experimently-selfhost exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > ~/controlvar-backups/controlvar.dump
cp .env ~/controlvar-backups/controlvar.env
```

The dump is made inside the `postgres` service with the user and database
that service was started with (the defaults in `compose.yml`, unless you set
`POSTGRES_USER` and `POSTGRES_DB`), and written to the file you redirect it to
on the host.

`.env` has to travel with the dump. `generate-env.sh` writes a note at the top
of it that its secrets cannot be regenerated. `SECRET_KEY` signs the sign-in
tokens, and `AUDIT_HMAC_KEY` signs the audit records of the `full` profile:
run `generate-env.sh` again on a new host and it makes different keys.

Redis needs no backup. In this stack it holds the rate-limit counters and
response caches, which the API rebuilds, and the API runs without it
(`REDIS_REQUIRED` is `false`). It writes an append-only file to its own
volume, so a restart keeps its data.

### Restore

Restore into a fresh stack of the same image tag the dump came from: the same
release, because the database records the revision it was migrated to, and
the same profile. An older image refuses to start on a database a newer release
migrated (see
[Database migrations](https://docs.controlvar.com/self-hosting/migrations/)),
so restore first and upgrade afterwards, the usual way.

On the new host, put that release's `compose.yml` and the saved `.env` in one
directory, and do not run `generate-env.sh`. Do not run the commands below in
the directory of a stack that is running: the first replaces its `.env`, and
the restore loads into whatever database its `postgres` service holds. Then:

```bash
cp ~/controlvar-backups/controlvar.env .env
docker compose -p experimently-selfhost up -d --wait postgres
docker compose -p experimently-selfhost exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error' \
  < ~/controlvar-backups/controlvar.dump
docker compose -p experimently-selfhost up -d --wait
```

Only PostgreSQL is started first, so the database it creates is still empty
when the dump is loaded: the API creates the schema on its first start.
When the API then starts it finds the restored schema and the restored users,
and creates neither a schema nor a first administrator.

To restore on the host that runs the stack, stop it with
`docker compose -p experimently-selfhost stop`, which keeps its volumes, and
restore under another project name: the same commands with a different `-p`
start with empty volumes.

## What CI runs

The `compose-production` workflow runs this file on every pull request that
changes it, with that change's images under the published names. It runs
`generate-env.sh` twice, and the second run must refuse and leave `.env` as it
was. It starts the stack with `docker compose up -d --wait` and signs in as
the first administrator through the dashboard's port. It checks that a request
for another host name answers `400`, that the demo login answers `401`, and
that PostgreSQL, Redis and the API publish no port.
