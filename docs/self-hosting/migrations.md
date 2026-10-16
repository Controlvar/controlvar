# Database Migrations

The platform uses **Alembic** for PostgreSQL schema management. Each migration is a versioned script, with an `upgrade()` and a `downgrade()` function.

---

## Overview

Migration files live in `backend/app/db/migrations/versions/`. Each file represents one atomic schema change, identified by a unique revision ID. Alembic tracks which migrations have been applied in the `alembic_version` table in your database.

**Two branches, two heads.** A full-profile checkout also has
`modules/backend/app/db/migrations/versions/`, a separate branch labelled
`modules` for the schema the optional modules own. A core checkout has no
`modules/` directory and therefore one head; a full checkout has two, and
`alembic_version` holds one row per head. That is why every command below says
`heads` (plural) and never `head`: alembic refuses the singular when more than
one head exists.

**Where the commands run.** Every alembic and bootstrap command on this page runs
inside the API container, with the image's own alembic and the container's
`POSTGRES_*` settings: `docker compose exec api ...`, from the directory of your compose file
(the repository root for the [Quick Start](../getting-started/quick-start.md)
stack, `deploy/compose/` for the production compose file). The image keeps the
repository's layout under its working directory `/app`, so
`-c backend/app/db/alembic.ini` is the same path there as in a checkout. Under
Helm, run the same command in an API pod with `kubectl exec`. On AWS, see
[Running Migrations in Production](#running-migrations-in-production).
Running alembic from a checkout instead is a contributor's task:
[Database Migrations with Alembic](../development/database/migrations.md#database-migrations-with-alembic).
CI runs this page's `exec` blocks as written against the Quick Start stack.

---

## Running All Migrations

The API container runs the bootstrap (below) every time it starts, so a stack
that is up is already at its heads. To apply all pending migrations by hand:

```{.bash exec}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini upgrade heads
```

`heads` refers to the latest migration of every branch. This command applies all
unapplied migrations in order. Alembic logs what it applies (to stderr); on a
database already at its heads it applies nothing.

On a database with no tables at all, run the bootstrap instead — the historical
migration chain cannot be replayed from zero, so a fresh schema is created from
the models and stamped. On a database that has tables it runs `upgrade heads`:

```{.bash exec}
docker compose exec api python -m backend.app.db.bootstrap
```
<!-- expect: Database bootstrap complete -->

It ends with `Database bootstrap complete (created)` on a new database and
`Database bootstrap complete (upgraded)` on one that had tables.

The bootstrap reads `ENVIRONMENT` from the container, which compose sets
(`development` unless `ENVIRONMENT` was set when the stack was started); for
another value, pass it to that one command:
`docker compose exec -e ENVIRONMENT=production api python -m backend.app.db.bootstrap`.
`ENVIRONMENT` is the environment the database belongs to (`production`,
`staging`, or `development` for a local trial). On a database with no users the
bootstrap creates the first administrator from `FIRST_SUPERUSER` and
`FIRST_SUPERUSER_PASSWORD`, and it refuses a password that is empty, shorter
than 8 characters or a well-known default unless `ENVIRONMENT` is
`development` or `test`. In every environment it also refuses a password
longer than 72 bytes when encoded as UTF-8 (a character outside ASCII takes two
to four bytes). Once the database has users the setting is not read.

The bootstrap is also what the API container runs on start-up when
`RUN_MIGRATIONS=true`, the image's default, and it is what makes a **profile
switch** work; see "Switching profile" below. The AWS (CDK) deployment sets
`RUN_MIGRATIONS=false` on its API tasks: there, the Deploy workflow's migration
task is the only thing that runs the bootstrap (see
[On AWS, the API does not migrate](#on-aws-the-api-does-not-migrate)).

---

## Viewing Migration History

Show every migration the image carries, newest first:

```{.bash exec}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini history
```
<!-- expect: (head) -->
<!-- expect: <base> -->

Output, for example:

```text
37dcb2969766 -> 4c2c09a2b3e8 (head), Safety rollback records: safety_config_id nullable, its foreign key ON DELETE SET NULL
d29a479daafe -> 37dcb2969766, Segments: a kind (rules or id_list), and the members of an id list
...
<base> -> 84a772608a6e, Initial schema
```

`(head)` marks the newest revision of each branch in the image; a full image
also lists the `modules` branch, labelled `(modules)`. `history` lists the
image's revisions whether or not the database has them: `current` (next) says
what the database has.

---

## Checking Current State

Show which migration is currently applied to the database:

```{.bash exec}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini current
```
<!-- expect: (head) -->

Output, for a full install (a core install prints the first line only):

```text
4c2c09a2b3e8 (head)
modules_0002_warehouse_analysis (head)
```

One line per head: the revision ID of the last migration applied on each
branch, and `(head)` when it is the newest the image has.

---

## Creating a New Migration

A migration is written in a checkout and ships in a release; it is not created
against a deployment. See
[Creating New Migrations](../development/database/migrations.md#creating-new-migrations).

---

## Applying a Specific Migration

Apply migrations up to a specific revision (not necessarily the latest), named
by its ID from `history`:

```{.bash skip reason="fragment: <revision> is a revision ID from history"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini upgrade <revision>
```

Name the revision rather than a relative step such as `upgrade +1`. A relative
step counts along one line of revisions, and with two heads alembic refuses it:
`Relative revision +1 didn't produce 1 migrations` (measured from a full checkout
against a database at the core head, with the `modules` branch still to apply).

---

## Rolling Back

Roll back the most recent migration **of one branch**. With two heads a bare
`-1` is ambiguous — alembic warns and picks one, which may not be the branch you
meant — so name the branch:

The modules branch has two revisions: `modules_0001_rbac` and, after it,
`modules_0002_warehouse_analysis`. `modules@-1` unapplies the newest one only;
`modules@-2` unapplies both, newest first.

The modules branch, one revision back (modules_0002_warehouse_analysis):

```{.bash skip reason="destructive: unapplies modules revisions and drops the tables they created"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini downgrade modules@-1
docker compose exec api python -m alembic -c backend/app/db/alembic.ini downgrade modules@-2
```

One step back on the core chain:

```{.bash skip reason="fragment: <core revision id> is the ID before the core head, from history"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini downgrade <core revision id>
```

**Not `modules@base`.** `modules_0001_rbac` is a child of the core revision
`a7b8c9d0e1f2`, not an alembic base, and with a single tree root alembic cannot
filter a downgrade by branch label: `downgrade modules@base` resolves to **the
whole core chain** to base, and drops every table in the schema.

Each modules revision downgrades only the objects its own `upgrade()` created
(it marks them with a PostgreSQL COMMENT as it goes). On a database whose
tables came from the bootstrap rather than from those migrations, their
downgrade is a no-op — it will not drop populated tables it never made.

### `modules_0002_warehouse_analysis` removes saved warehouse connections

This revision replaces the `warehouse_connections` table with a new one and
adds `warehouse_sources` and `warehouse_analysis_runs`. **Every row of the
earlier `warehouse_connections` is deleted** — connections saved through the
earlier warehouse endpoints — and nothing is copied into the new table.

Downgrading it cannot bring those rows back: `downgrade modules@-1` drops the
three tables it created and puts back an **empty** table of the earlier shape.
If you need the rows, take a snapshot of the database before upgrading to the
release that carries this revision, and restore that snapshot to go back. The
migration logs how many rows it removed:

```text
modules_0002: removing 3 legacy warehouse connection rows
```

### `1ab99332f0ba` rewrites stored event times to UTC

`events.created_at` is stored as text, so time windows, ordering and "the
earliest conversion" over events compare text. Events stored before this
release kept the time exactly as the client sent it: with its own offset
(`2026-10-01T01:00:00+02:00`), with `Z`, or with no offset at all. As text,
those sort among each other by the digits, not by the moment, so an event could
fall outside a time window it belongs in, and the wrong event could count as a
user's first conversion. Events written by this release are already stored in
one form, in UTC.

This revision rewrites every stored value that is not in that form to the same
instant in it: `2026-10-01T01:00:00+02:00` becomes `2026-09-30T23:00:00+00:00`
(`.ffffff` is kept when there are microseconds). A value with no offset is read
as UTC, as the API reads it, and gets `+00:00`. Values already in the form are
not touched, and no other column changes. A value that cannot be read as a time
(an empty string, text that is not a date, or a date out of range) is left
exactly as stored and counted.

What readers of results see: a converting user's first-conversion day in the
daily results can move by one day. Totals, rates and significance do not
change.

**The downgrade cannot bring back the offsets clients sent.** Each row keeps
its instant; the offset it arrived with is not recorded anywhere. If you need
the original text, take a snapshot of the database before upgrading, and
restore that snapshot to go back. A previous release runs against the rewritten
rows unchanged: the column type is the same and the values are in the form it
writes for its own timestamps.

The migration prints one line when it finishes. It names event ids, never the
stored values:

```text
1ab99332f0ba: rewrote 1520 events.created_at values to UTC; 2 unreadable values left as stored (event ids: 0b5c6c2e-6f0e-4a1b-9c55-2f4f1f0f3a10, 7d1e2f30-1a2b-4c3d-8e9f-0a1b2c3d4e5f); 4.3 s
```

**Before upgrading, under Docker Compose or Helm: how many rows will it
rewrite?** Run this read-only count with `psql` (see
[Resolve each group](#resolve-each-group) for how to open it). It is the
migration's own selection, copied exactly. The statements here use the schema
`experimentation`; if your `POSTGRES_SCHEMA` is different, replace it.

```sql
-- Count the event times the upgrade will rewrite
SELECT count(*) FROM experimentation.events
 WHERE created_at !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?\+00:00$'
   OR created_at LIKE '%.000000+00:00';
```

If it returns 0, the migration changes nothing. Otherwise it scans the table
once and rewrites the rows it counted. How long that takes depends on your
database: the first measure of it is your own staging run's log line.

**After upgrading, under Docker Compose or Helm:** this lists what is left. Expect
exactly the unreadable values the log line counted, which the migration leaves
as they were:

```sql
-- List the event times left after the upgrade
SELECT id FROM experimentation.events
 WHERE created_at !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?\+00:00$'
   OR created_at LIKE '%.000000+00:00';
```

**A long run is not a failed run. Do not kill it.** The rewrite commits batch
by batch, so a run that is stopped keeps every batch it finished, and the next
upgrade finishes the rest. Killing it only makes you wait twice.

- **Docker Compose.** The API container runs the migration on start and serves
  once it has finished. If the migration runs longer than about 10 minutes,
  `docker compose up` reports the API unhealthy and fails while the migration is
  still running in the container. Do not restart it: wait for the line above in
  `docker compose logs api`.
- **Helm.** The `migrate` init container runs it while the old pods keep
  serving. `helm upgrade --wait --timeout 10m` can report a failure while the
  init container is still running. Do not delete the pod: wait for the line in
  `kubectl logs <pod> -c migrate`.
- **AWS.** The Deploy workflow's migration task runs it before traffic shifts,
  and the previous release keeps serving meanwhile. The AWS deployment gives you
  no `psql`, so use the migration's log line instead of the queries above, and
  read the staging deploy's line (rewritten, unreadable, seconds) before you
  deploy production. The Deploy workflow waits 30 minutes for the migration, and
  no setting changes that for one deploy. A longer run fails the deploy with
  **Migration still running**, and nothing shifts. Do not start another deploy
  yet: wait until the task named in that error has stopped (the error names its
  log group and stream; the ECS console shows the task's status as `STOPPED`),
  then run the Deploy workflow again for the same tag. Its migration finishes
  the rest.

**Events written while the upgrade ran.** Under Helm and on AWS the previous
release keeps serving, and keeps storing times as clients send them, until the
new release takes all the traffic. Rows it writes after the migration's scan
stay as they were. Once the new release serves all traffic, run the migration
again: downgrade to the revision before it and upgrade. The second log line says
how many values it rewrote.

**This re-run applies only while `alembic current` shows `1ab99332f0ba` as the
core head** (with `modules_0002_warehouse_analysis` on a full install). Then the
downgrade changes no data; it only moves the version row back. Complete it
before you upgrade to the release that adds `806901fb7735`, the experiments'
stored correction settings. Past that release the recipe is not offered:
`downgrade a89544fb1075` would also run `806901fb7735`'s downgrade, which drops
every experiment's stored correction method and confidence level, and the
`upgrade heads` after it puts back the defaults (Benjamini-Hochberg, 0.95), not
the choices that were stored.

```{.bash skip reason="destructive: steps the schema back a revision before running it again"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini downgrade a89544fb1075
docker compose exec api python -m alembic -c backend/app/db/alembic.ini upgrade heads
```

On AWS these are two runs of the Database Migration workflow: direction
`downgrade` with target `a89544fb1075`, then direction `upgrade` with target
`heads`.

**Name `a89544fb1075`; do not use `-1`.** On a full install `downgrade -1` can
step back the modules branch instead, and that downgrade drops the warehouse
tables `modules_0002_warehouse_analysis` created. The Database Migration
workflow refuses `-1`.

Past `d29a479daafe` that downgrade would also drop the table
`holdout_population`, with every row in it, and three `global_holdouts` columns
(see the next section).

Past `37dcb2969766` it also runs that revision's downgrade, which drops the
table `segment_members` (the members of every id-list segment) and the column
`segments.kind`. **So it now stops while any id list has members**: that
downgrade refuses, prints how many rows `segment_members` holds, and changes
nothing, because nothing could put the members back and every id-list segment
would return as a rules segment with no rules (see
[`37dcb2969766`](#37dcb2969766-adds-segments-made-from-a-list-of-user-ids)).

### `d29a479daafe` records who each global holdout covers

This revision adds `activated_at`, `deactivated_at` and `hash_salt` to
`global_holdouts`, a partial unique index that allows at most one active
holdout, and the table `holdout_population`, which records each user first seen
while a holdout is active and whether the holdout kept them out.

**It deactivates all but one active holdout.** Before the index is created,
every active holdout except the most recently updated one is deactivated and
stamped as ended; an ended holdout cannot be activated again. The kept row may
not be the one the previous release was enforcing. The migration prints the
ids it deactivated:

```text
d29a479daafe: deactivated 1 global holdout(s) to keep one active: 7d1e2f30-1a2b-4c3d-8e9f-0a1b2c3d4e5f
```

Before upgrading, this read-only query lists the active holdouts; with more
than one row, the first is the one kept:

```sql
-- List the active holdouts, the kept one first
SELECT id, name, is_active, updated_at FROM experimentation.global_holdouts
 WHERE is_active ORDER BY updated_at DESC, id;
```

**The holdout active at the upgrade is never measurable.** It keeps the salt
every holdout used before (so users stay in or out of it exactly as before),
and who it kept out before the upgrade was never recorded. To measure, deactivate
it and create a new holdout.

**The downgrade deletes the recorded memberships.** `d29a479daafe`'s downgrade
drops `holdout_population` with all its rows, the index, and the three columns;
nothing can rebuild the table. The deactivations are not reverted. Rolling back
across this revision while a holdout is active also invalidates that holdout:
the older release buckets users with the old salt and records nobody, so after
upgrading again deactivate it and create a new one.

### `37dcb2969766` adds segments made from a list of user ids

This revision adds `segments.kind` (`rules` or `id_list`, default `rules`, with
the check `ck_segments_kind`) and the table `segment_members`, one row per user
id in an id-list segment. Every segment already stored becomes a `rules`
segment, which is what it was; no row is changed.

**The downgrade deletes every id list's members.** `37dcb2969766`'s downgrade
drops `segment_members` with all its rows, the check and `segments.kind`;
nothing can rebuild the table, and an id-list segment comes back from a later
upgrade as a rules segment with no rules. So while `segment_members` has any
row the downgrade refuses, names the row count, and changes nothing:

```text
37dcb2969766: refusing to downgrade: segment_members holds 3 row(s), ...
```

To go on, either remove the members first (`POST
/api/v1/segments/{id}/members/remove`), or, once someone has agreed to lose
them, run the same downgrade with the override:

```{.bash skip reason="destructive: drops every id-list segment's members"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini -x allow_member_loss=true downgrade d29a479daafe
```

The Database Migration workflow does not pass `-x`, so on AWS it stops at this
revision while any id list has members.

### `4c2c09a2b3e8` keeps rollback records without a safety configuration

This revision makes `safety_rollback_records.safety_config_id` nullable and
changes its foreign key to `feature_flag_safety_configs` from ON DELETE CASCADE
to ON DELETE SET NULL. It changes no row.

From this release a rollback of a flag that has no safety configuration of its
own leaves it with none, so the flag stays on the global default thresholds,
and the rollback record's `safety_config_id` is null. Earlier releases gave such
a flag an empty configuration of its own at the rollback, with no thresholds, so
the monitor stopped checking it against the defaults. Those configurations
stay: this release does not remove them
([#1206](https://github.com/Controlvar/controlvar/issues/1206)).

Deleting a safety configuration keeps the flag's rollback records: their
`safety_config_id` becomes null. Before this revision the delete took them with
it.

**The downgrade changes nothing.** `4c2c09a2b3e8`'s downgrade moves
`alembic_version` back and leaves the column nullable and the key ON DELETE SET
NULL. The rollback runbook undoes a migration while the release that contains it
still serves, before the API is rolled back
([Database Rollback Procedure](../deployment/rollback-runbook.md#database-rollback-procedure)),
and this release records a null `safety_config_id` for each rollback of a flag
on the defaults, which NOT NULL would refuse.

Roll back to a specific revision:

```{.bash skip reason="fragment: <revision> is a revision ID from history"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini downgrade <revision>
```

Roll back all migrations (to the empty database state):

```{.bash skip reason="destructive: drops every table in the schema"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini downgrade base
```

**Note**: Not all migrations are safely reversible. If a migration deletes a column, the downgrade drops data. Review the `downgrade()` function in each migration file before rolling back in production. A migration that rewrites data may not be able to undo it at all: see [`1ab99332f0ba` rewrites stored event times to UTC](#1ab99332f0ba-rewrites-stored-event-times-to-utc).

---

## Stamping Without Running Migrations

The `stamp` command marks a migration as applied without actually running its SQL. Use this when:

- You have manually applied schema changes and want to bring Alembic in sync
- You are setting up Alembic on an existing database
- You need to skip a problematic migration after fixing it manually

Mark current database state as "head":

```{.bash skip reason="destructive: records revisions as applied without running them"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini stamp heads
```

Mark as a specific revision:

```{.bash skip reason="fragment: <revision> is a revision ID from history"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini stamp <revision>
```

---

## Resolving "Multiple Heads" Errors

On a full image, a command that names the singular `head` fails:

```text
FAILED: Multiple head revisions are present for given argument 'head'
```

Name `heads` (plural) instead. To see the heads the image has:

```{.bash exec}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini heads
```
<!-- expect: (head) -->

A release image prints its core head, and a full image the `modules` head too,
labelled `(modules)`.

---

## Environment Variables

Alembic and the bootstrap connect with these settings. The defaults below are the
Quick Start compose file's:

| Variable | Quick Start default | Description |
|----------|---------|-------------|
| `POSTGRES_SERVER` | `postgres` | Database host (the compose service; `localhost` from the host) |
| `POSTGRES_PORT` | `5432` | Database port |
| `POSTGRES_USER` | `postgres` (`experimently` in `deploy/compose/compose.yml`) | Database username |
| `POSTGRES_PASSWORD` | `postgres` (none in `deploy/compose/compose.yml`, which requires it) | Database password; set your own outside a local trial |
| `POSTGRES_DB` | `experimentation` | Database name |
| `POSTGRES_SCHEMA` | `experimentation` | PostgreSQL schema name |

The database server's `TimeZone` must be `UTC`, as it is in the bundled PostgreSQL of both
compose files and the Helm chart. On another time zone, some times the API stores move by the
zone's offset: an experiment's `updated_at`, for example. Its start and end dates are stored in
UTC whatever the zone.

To see the bundled database's time zone:

```{.bash exec}
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SHOW TimeZone"'
```
<!-- expect: UTC -->

It prints `UTC`.

---

## Migration Guidelines

### Do

- Generate migrations with `--autogenerate` and always review the output
- Test migrations on a copy of production data before applying to production
- Include both `upgrade()` and `downgrade()` functions
- Use descriptive migration names: `add_bayesian_config_to_experiments`
- Commit migration files to version control alongside the model changes that require them
- **Expand, then contract.** A migration in release N must not drop or rename
  anything release N-1's code uses. Add the new column or table in one release,
  move the code over, and drop the old one in a later release. During a
  rolling upgrade, release N-1 keeps serving against release N's schema after
  release N's migration has run.

### Do Not

- Edit migration files that have already been applied to production
- Use bare revision IDs in `down_revision` — always use the actual ID string, not a migration name
- Delete migration files from the versions directory
- Apply migrations without setting the correct `POSTGRES_SCHEMA` environment variable (the schema namespace will be wrong)

---

## Running Migrations in Production

In production (ECS Fargate), migrations run as a one-off ECS task of the family
`experimentation-migrate-<env>`, before the new application version takes any
traffic. You do not normally run it yourself: the Deploy workflow runs it, and
the Database Migration workflow (`.github/workflows/db-migrate.yml`, Actions ›
Database Migration) runs an `upgrade` or a `downgrade` to a target you name, after
taking a snapshot. Both run it the same way:

1. register a new revision of `experimentation-migrate-<env>` whose `backend`
   container runs the image being deployed (the Database Migration workflow: the
   image the API is serving), named by its digest
   (`scripts/register_task_definition.sh`);
2. run **that revision's ARN**, never the bare family, which resolves to
   whichever revision registered last, wait for it to stop, and print its log
   (`scripts/run_migration_task.sh`).

The task's command is `MIGRATION_COMMAND` in
`infrastructure/cdk/stacks/migration_task_stack.py`: the bootstrap, `python -m
backend.app.db.bootstrap`, which creates the schema of an empty database and runs
`upgrade heads` on one with tables. The Database Migration workflow overrides it
with `python -m alembic -c backend/app/db/alembic.ini <direction> <target>`. The
path is relative to the image's `WORKDIR /app`, under which `backend/Dockerfile`
copies the repository layout unchanged.

By hand, from a checkout with AWS credentials for the environment, the Deploy
workflow's two steps are these. `IMAGE` is the image by digest
(`<registry>/<repository>@sha256:<digest>`), and `NETWORK` the task's network
configuration, from the Fargate stack's `TaskSubnets` and `TaskSecurityGroup`
outputs:

```{.bash skip reason="aws: registers and runs a task in your AWS account"}
ARN=$(bash scripts/register_task_definition.sh experimentation-migrate-staging "$IMAGE")
bash scripts/run_migration_task.sh experimentation-staging "$ARN" "$NETWORK" \
  '{"containerOverrides":[{"name":"backend","command":["python","-m","backend.app.db.bootstrap"]}]}' \
  /ecs/experimentation-migrate-staging
```

---

## Switching Profile

The same database can be built by one profile and opened by the other. Both
documented paths handle it the same way: `python -m backend.app.db.bootstrap`
with `ENVIRONMENT` set (what the API container runs on start-up) and `alembic upgrade heads` (what the
migration task runs) share the repairs below, because `backend/app/db/migrations/env.py` calls
them after a command-line upgrade.

**Core database, full image.** The core chain marks the revisions that once
created `workspaces`, `sso_configs`, `phi_audit_logs` and the rest as applied
without creating them, and the `modules` branch does not re-create them (it owns
the three RBAC tables only). So after the upgrade reaches this build's heads,
the module tables the models declare and the database lacks are created from
the models, the two `workspace_id` foreign keys are added, and a WARNING says
so. Only module tables are ever created this way — an unmigrated *core* model is
not, so a missing core migration still shows up as a missing table rather than
being papered over. `alembic upgrade heads --sql` (offline mode) has no
reconcile step: the SQL it emits for a profile switch is incomplete.

**Full database, core image.** `alembic_version` holds a revision a core build
has no file for. `alembic upgrade heads` answers in one of two ways: when the
core chain is already at its head there is nothing to apply, so it skips alembic,
leaves the rows untouched and logs a WARNING naming the foreign revisions (exit
0); when the core chain is *behind* — a newer core image against a database the
full image built — it refuses with a message naming them. `downgrade` and
`stamp` do not skip: they fail with alembic's own "Can't locate revision
identified by 'modules_0002_warehouse_analysis'" (or `modules_0001_rbac`, on a
database a release before the warehouse tables migrated), and the schema is
untouched.

The escape hatch, when you really do want the core image to own that database:
take a backup, then from the core image

```{.bash skip reason="destructive: replaces the database's recorded revisions"}
docker compose exec api python -m alembic -c backend/app/db/alembic.ini stamp --purge heads
```

`--purge` replaces the whole version table with this build's own heads, so the
modules row goes and the core row stays. The module tables themselves are never
dropped by switching down to core. They stay, unused, until the database goes
back to the full profile.

Use it only when the core chain is already at this release's head, which is
the case where `upgrade heads` skips with a WARNING. `stamp` records revisions without
running them, so after the refusal above it would mark this release's pending
core migrations as applied when they are not. Run the full image of this
release first, then stamp.

---

## An Older Image Against a Newer Database

Rolling back to an older image after a release that carried a migration leaves
the database recorded at a revision the older image has no file for. Both
documented paths refuse, exit non-zero and leave the database untouched, with a
message naming the revision and the image's version: *this database was
migrated by a newer Controlvar release*. The two ways out are the ones it
names:

- run the newer release that migrated the database, or
- restore the backup taken before that upgrade, and run the older image
  against the restored database.

Do not edit `alembic_version` to make the older image start.

The refusal is at start-up, so instances of the older image that were
already running keep running. That, and the window in every rolling upgrade
where the previous release still serves after the new release's migration has
run, is why a migration must not drop or rename anything the previous
release's code still uses (see *Migration Guidelines*).

### On AWS, the API does not migrate

On the AWS (CDK) deployment the API task definition sets `RUN_MIGRATIONS=false`
and `SEED=` (empty), for every environment and both profiles. An API task never
runs the bootstrap; the only thing that writes the schema is the migration task
the Deploy workflow runs before it shifts traffic (or the Database Migration
workflow, run by hand). `infrastructure/tests/test_api_task_does_not_migrate.py`
pins both values.

Why: an image refuses to start against a database a newer release has
migrated (above). While the API ran the bootstrap on every start, any API task
started from an older revision after a Deploy's migration -- a replaced task, a
scale-out, a rollback -- exited instead of serving. With the bootstrap out of
the API task, an older revision starts against the newer schema and serves it.

What this means for you:

- **`cdk deploy` alone no longer creates the schema.** After the first
  `cdk deploy` of an environment, the API runs against an empty database until
  the first Deploy creates it. In that window
  `/health` answers 200 -- it checks that the database answers, not that the
  schema is there -- so the load balancer's health checks pass, while real
  requests answer 500 and the API logs errors. That is expected until the first
  Deploy.
- **The first Deploy may be refused by the API's 5xx alarms.** Anything that
  reaches the schema-less API in that window, a scanner included, can put
  `experimentation-api-5xx-blue-<env>` or `-green-<env>` into ALARM, and the
  Deploy refuses to start while one is. The way through is Deploy's existing
  break-glass
  ([rollback runbook, "Fix forward while an alarm is firing"](../deployment/rollback-runbook.md#fix-forward-while-an-alarm-is-firing)),
  and whether to use it is a person's decision at that moment. Keep the time
  between the first `cdk deploy` and the first Deploy short.
- **Rolling back relies on backward-compatible migrations.** A rollback runs the
  older release against the newer schema. That works only when the migrations
  in between are backward-compatible (see *Migration Guidelines*). For one that
  is not -- a dropped or renamed table or column the older release reads --
  undo the migration while the release that contains it is still serving, and
  only then roll the API back
  ([rollback runbook, Database Rollback Procedure](../deployment/rollback-runbook.md#database-rollback-procedure),
  [Step 2](../deployment/rollback-runbook.md#step-2-run-migration-downgrade-via-github-actions)). Once the API has been rolled back, the workflow
  can no longer undo it; the emergency route is then a point-in-time restore to
  a new cluster, which the API tasks cannot pick up today
  (the same procedure, Step 3).
  Some data cannot come back without such a restore: `modules_0002_warehouse_analysis`,
  which shipped in 0.11.0, drops the earlier `warehouse_connections` table with
  its rows, and neither a rollback nor a downgrade restores them. And
  [`1ab99332f0ba`](#1ab99332f0ba-rewrites-stored-event-times-to-utc) rewrites
  stored event times to UTC: a rollback runs against those rows unchanged, but
  the offsets clients sent come back only from a restore to a time before it
  ran. [`d29a479daafe`](#d29a479daafe-records-who-each-global-holdout-covers)
  is backward-compatible for a rollback, but undoing it drops
  `holdout_population` and its rows.
  [`37dcb2969766`](#37dcb2969766-adds-segments-made-from-a-list-of-user-ids)
  is backward-compatible for a rollback too, and undoing it drops
  `segment_members` and its rows; it refuses while that table has any.
  [`4c2c09a2b3e8`](#4c2c09a2b3e8-keeps-rollback-records-without-a-safety-configuration)
  is backward-compatible for a rollback, and undoing it changes nothing.
- Compose runs one API container, which is the only writer and keeps `RUN_MIGRATIONS=true`.
  The chart already keeps the bootstrap out of the serving container: it runs
  it in an init container.

An environment whose Fargate stack was deployed before this change still has
API revisions that run the bootstrap on start. Those revisions keep doing so,
and a rollback to one of them after a newer migration refuses to start. Deploy
the Fargate stack from a checkout with this change before relying on rollback.

The Deploy workflow checks the revision it is about to deploy. After it
registers the API revision it reads back what ECS stored, and refuses to create
the CodeDeploy deployment unless the `backend` container sets
`RUN_MIGRATIONS=false` exactly. By then the migration has run; the revision
serving before keeps serving. The refusal names the fix, a `cdk deploy` of the
Fargate stack from a checkout with this change
([rollback runbook](../deployment/rollback-runbook.md#deploy-refused-an-api-revision-that-would-run-migrations-on-start)).
Rollback does not check its target in the same way; the runbook gives the
command to check one by hand.

---

## Email addresses that differ only in case

From this release an email address belongs to one account whatever its letter
case: `Bob@acme.com` and `bob@acme.com` are the same address, and the database
refuses a second account holding it (the unique index `ix_users_email_lower`).
A database can already hold two such accounts if an administrator, SQL or a
restore created them. The upgrade then stops before it changes anything:

```text
Refusing to upgrade schema experimentation: 2 groups of accounts have email addresses that differ only in letter case, and from this release an email address belongs to one account whatever its case.
Accounts that share an address (user ids, one group per line, oldest first):
  11111111-1111-1111-1111-111111111111 22222222-2222-2222-2222-222222222222
  33333333-3333-3333-3333-333333333333 44444444-4444-4444-4444-444444444444
Nothing has been changed: the database is still at the revision it had before this upgrade, and the release you upgraded from runs against it as before.
See docs/self-hosting/migrations.md#email-addresses-that-differ-only-in-case
```

The message names account ids only, never an address. It lists at most 20
groups, then `... and K more groups`; the listing query below shows them all.
Every pending migration of that upgrade is rolled back with it, so the
database is exactly as the release you upgraded from left it.

**Where you see it, and how to keep serving:**

- **Docker Compose.** The API container runs the upgrade on start, exits, and
  restarts until the database is fixed: `docker compose logs api --tail 100`.
  To serve meanwhile, start the previous release's image; nothing was migrated,
  so it starts normally.
- **Helm.** The `migrate` init container fails and the rolling update stalls;
  the old pods keep serving. `kubectl logs <pod> -c migrate`.
- **AWS.** The Deploy workflow's migration step fails and nothing shifts; the
  previous release keeps serving. The step prints the migration task's last
  100 log lines as **one tab-separated line**, so the message above appears
  inside that line, ending with the
  `docs/self-hosting/migrations.md#email-addresses-that-differ-only-in-case`
  link. The task's log stream in CloudWatch shows it line by line.

### Resolve each group

For each group, choose the account that keeps the address and retire the
others. Do this with `psql`, using the API's `POSTGRES_*` settings. Under Docker
Compose, from `deploy/compose/` (or the repository root for the Quick Start
stack), this opens it; it reads statements until you type `\q`:

```{.bash exec}
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The statements below use the schema `experimentation`. If your
`POSTGRES_SCHEMA` is different, replace it.

1. **List every group.** Save this output somewhere private before you change
   anything: its `email` column is the record of each address as it was, which
   is what you need to undo a step by hand.

   ```sql
   -- List the accounts in each group
   SELECT lower(email) AS address, email, id, username, created_at, is_active,
          is_superuser, role, hashed_password IS NOT NULL AS has_password
     FROM experimentation.users
    WHERE lower(email) IN (SELECT lower(email) FROM experimentation.users
                            WHERE email IS NOT NULL
                            GROUP BY lower(email) HAVING count(*) > 1)
    ORDER BY lower(email), created_at;
   ```

2. **Choose the account to keep.** Keep the account the person actually signs
   in with; if you are not sure, ask them. There is no last-sign-in column to
   go by.

3. **Give the kept account the role it needs.** If the account you are about to
   retire has the role or the superuser flag the person needs, copy them to the
   account you keep first. Replace both ids with ids from the listing:

   ```sql
   -- Give the kept account the role of the one you retire
   UPDATE experimentation.users AS kept
      SET role = retired.role, is_superuser = retired.is_superuser
     FROM experimentation.users AS retired
    WHERE kept.id = '<id to keep>' AND retired.id = '<id to retire>';
   ```

4. **Retire the other account.** It keeps its history and everything it owns,
   but it can no longer sign in and no longer holds the address. Its address
   becomes `retired-<its id>@<its own domain>`:

   ```sql
   -- Retire the other account
   UPDATE experimentation.users
      SET email = 'retired-' || id || '@' || split_part(email, '@', 2),
          is_active = false
    WHERE id = '<id to retire>';
   ```

   The retired address keeps the account's own domain because the API refuses
   addresses under reserved domains such as `.invalid`, `.test` or `.local`; an
   account given one could no longer be edited through the API.

   `email` holds at most 100 characters, and `retired-<id>@` takes 45 of them,
   so this works when the domain is at most **55 characters**. For a longer
   domain the statement fails with `value too long for type character
   varying(100)` and changes nothing. Then either use the shorter form below,
   which fits a domain of up to **61 characters**, or give the account an
   address its owner actually holds (with the SQL above, or with
   `PUT /api/v1/admin/users/{id}` from the release that is still serving,
   which needs `username` and `email` in the body):

   ```sql
   -- If the domain is longer than 55 characters
   UPDATE experimentation.users
      SET email = 'r-' || id || '@' || split_part(email, '@', 2),
          is_active = false
    WHERE id = '<id to retire>';
   ```

5. **Check that no group is left.** This returns no rows when you are done:

   ```sql
   -- Check that no group is left
   SELECT array_agg(id ORDER BY created_at) AS ids
     FROM experimentation.users
    WHERE email IS NOT NULL
    GROUP BY lower(email) HAVING count(*) > 1;
   ```

6. **Run the upgrade again** the way you ran it the first time. It builds the
   index itself.

**Do not delete the account instead of retiring it.** Deleting a user deletes
or detaches what refers to it: its API keys, reports, notification
preferences, role assignments and workspace memberships are deleted with it,
and the experiments, flags, audit records and everything else that names it
lose that reference. Retiring keeps all of that.

**Do not build the index by hand.** Run the upgrade, which locks the table,
checks for groups and builds the index in one transaction. An index built
another way can be left half-built: a `CREATE UNIQUE INDEX CONCURRENTLY` that
fails on a group leaves an index that enforces nothing. The upgrade refuses to
continue past an index called `ix_users_email_lower` that is not exactly the
one it builds, and names the `DROP INDEX` to run before you try again.

**If the upgrade says another session is writing to `users`.** The upgrade
waits at most 30 seconds for the lock on `users`, so that it never holds up
the API's writes for long. Nothing has been changed; run it again when that
session has finished.
