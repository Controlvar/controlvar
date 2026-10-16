# Warehouse analysis (beta)

!!! note "Beta: Snowflake and BigQuery are available"
    Warehouse analysis runs an experiment's analysis on tables in your own
    data warehouse instead of on events sent to Controlvar. The API below is
    part of the **full profile** and every route is `x-stability: beta`: its
    shape may still change.

    Snowflake is available (see [Setting up Snowflake](#setting-up-snowflake)).
    BigQuery is available (see [Setting up BigQuery](#setting-up-bigquery)).
    Amazon Athena isn't available yet.
    `GET /api/v1/warehouse/analysis/connectors` says which are available on
    your deployment; creating a connection of any other type answers
    `422 connector_disabled`.

## What it does

You point Controlvar at two of your own tables or views:

- an **assignment source**: one row per exposure, with the unit id, the
  experiment key, the variant label and the time of the exposure;
- a **metric source** per metric: one row per event, with the unit id and the
  time of the event, and for a mean metric the event's value.

Controlvar writes the SQL itself (there is no field that takes SQL), runs
it as the identity you created for it, and computes the results
from what comes back. A metric is one of two types:

- a **proportion**: the share of exposed units that had at least one event in
  the conversion window, compared with Fisher's exact test, the statistics
  `/results` uses;
- a **mean**: the average per exposed unit of the sum of its event values in
  the conversion window (0 for a unit with no event; a NULL value is ignored
  and counted), optionally capped per unit with `cap_value`, and compared with
  Welch's t-test. Each variant reports its mean and a confidence interval.

Every run reports its sample-ratio check against the variants' traffic
allocation (skipped for multi-armed bandit experiments, whose split moves on
purpose).

### What leaves your warehouse

No row of your data. A run returns only aggregates:

- per variant label: the label (cut to 64 characters; at most 51 labels),
  the number of units, the number of converting units, and two sums;
- per statement: the grand mean and diagnostic counts (units seen in more
  than one variant, rows with no unit or variant, metric rows in the window
  and how many matched an exposed unit);
- the query's job id, bytes billed or elapsed time.

A preview returns counts, the number of units per variant label, and the
earliest and latest time in the window. Validation reads column names and
types from the warehouse's metadata; it queries no data.

**The connection's role decides what can be read.** Anyone who can author a
source can compute aggregates, and read short text such as variant labels,
over anything the connection's role can read. Grant that role only the tables
and views analysis needs, and read-only access to them.

### How it counts, and how that differs from `/results`

A unit's exposure is its first exposure in the analysis window. A unit
converts when it has an event at or after that exposure and within the
metric's conversion window (and before the end of the analysis window).
Units seen in more than one variant are left out and counted in the
diagnostics.

`/results` counts converting users with no bound on the exposure time and no
conversion window. So the two agree only on data with no event before a
unit's exposure, no unit in two variants and no event outside the window;
otherwise the warehouse numbers are the stricter ones.

## Setting up Snowflake

Controlvar signs in to Snowflake's SQL API as a user in your account, with
a key pair it generates. There is no password field.

1. **In Snowflake, give Controlvar a user and a role of its own.** Grant
   the role `USAGE` on the warehouse, the database and the schema, and `SELECT`
   on each table or view analysis reads, and nothing else.

    A connection whose role is ACCOUNTADMIN or another of Snowflake's
    built-in administrative roles is refused.
2. **Create the connection** in Warehouse › Connections (ADMIN). The account
   is the organisation-account identifier, for example `MYORG-MYACCOUNT`
   (Snowsight › Admin › Accounts), not a locator or a URL. Saving generates
   the key pair and shows the statement that registers its public key, for
   example `ALTER USER CONTROLVAR SET RSA_PUBLIC_KEY='…';`. Run it as an
   administrator. The private key never leaves Controlvar.
3. **Test the connection.** The test signs in and runs one statement that
   reads no table.
4. **Name tables in full** in a source: `DATABASE.SCHEMA.TABLE`, each part
   as Snowflake stores it (upper case unless it was created quoted).

Every statement runs with the session time zone set to UTC and is stopped by
Snowflake at the connection's `query_timeout_seconds`. A resource monitor on
the Snowflake warehouse is a good second limit.

To replace the key, choose **Generate a new key**. It shows the statement
that registers the new public key as `RSA_PUBLIC_KEY_2`; the old key keeps
working until a test with the new one passes.

## Setting up BigQuery

Controlvar signs in to BigQuery with a service-account key you create for
it, and runs every query as a job in a project you choose.

1. **In Google Cloud, create a service account for Controlvar** with these
   two roles only:
    - **BigQuery Job User** on the project that runs and pays for the
      queries (the connection's billing project);
    - **BigQuery Data Viewer** on each dataset analysis reads, granted on each
      dataset rather than on the project.
2. **Create a JSON key** for the service account and paste it into the
   connection form in Warehouse › Connections (ADMIN). It is sent only in the
   request body and stored encrypted, and it is never shown again. A key
   whose `token_uri` is not Google's own token endpoint is refused.
3. **Set the location** to where your datasets are: `US`, `EU`, or a region
   such as `europe-west2`.
4. **Set the bytes cap** ("Most data a query may read"). Before each query
   Controlvar runs a dry run: anything that is not a single `SELECT`, or
   that would read more than the cap, is refused before it runs. The real
   query is also sent with the cap as its billing limit, so BigQuery stops it
   rather than bill more.
5. **Test the connection**, then name tables in full in a source:
   `project.dataset.table`.

Every job carries the label `experimently: analysis`, so you can find its
cost in your billing.

## Who can do what

| Action | ADMIN | DEVELOPER | ANALYST | VIEWER |
|---|---|---|---|---|
| See which warehouses are available | yes | yes | yes | yes |
| List connections (never their credentials) | yes | yes | yes | no |
| Create, change, test or delete a connection | yes | no | no | no |
| Create, edit, validate, preview or delete an assignment source | yes | yes | no | no |
| Create, edit, validate or preview a metric source | yes | yes | yes | no |
| Delete a metric source | yes | yes | no | no |
| Start an analysis | yes | yes | no | no |
| Read analyses and their results | yes | yes | yes | no |
| Read the SQL an analysis or preview sent | yes | yes | yes | no |

A superuser counts as ADMIN. A preview is stored as a run, so reading an
analysis or a preview by its id (`GET /runs/{run_id}`) and listing an
experiment's analyses both need ADMIN, DEVELOPER or ANALYST; a VIEWER gets 403
`role_required` from both (D50). Everyone who can read a run also gets its
`statements`.

A refusal names the role needed and yours, for example
`Creating a warehouse connection requires the ADMIN role; you are DEVELOPER.`
Every change to a connection or a source, and every analysis started, is
recorded in the [compliance audit trail](compliance.md#what-is-recorded); a source's entry carries its full definition, and a
connection's entry never carries its credentials.

## Limits and cost

Every connection has limits, and each connection in the API shows its worst
case per day (`worst_case_bytes_per_day` or `worst_case_seconds_per_day`):

- **Per query**: a byte limit where the warehouse bills by bytes read, and a
  time limit (`query_timeout_seconds`, default 300) everywhere.
- **Per run**: a run is one diagnostics statement plus one statement per
  metric, at most 10 metrics. A run can cost at most (1 + metrics) × the
  per-query limit.
- **Per day**: `max_runs_per_day` (default 20) analyses **and previews** on the
  connection per UTC day, whatever their outcome. The count resets at 00:00
  UTC. The worst case for a day is `max_runs_per_day` × 11 × the per-query
  limit.
- **At once**: one analysis or preview per connection, and at most
  `WAREHOUSE_MAX_CONCURRENT_RUNS` (default 2) across the deployment.

Connection tests and source validation are not counted by the daily limit.
They are ADMIN-only (validation: the source's editors) and each has a
30-second limit.

When a limit is reached the API answers:

| Status | `code` | When |
|---|---|---|
| 429 | `daily_run_limit_reached` | The connection has used `max_runs_per_day` today. `Retry-After` is the seconds until 00:00 UTC, and the body carries `limit` and `resets_at`. |
| 429 | `warehouse_busy` | This deployment is already running as many warehouse jobs as it allows. `Retry-After: 30`. A refusal for capacity does not use a slot of the daily limit. |
| 409 | `run_in_progress` | An analysis or preview on this connection has not finished. |

## For operators

| Setting | Default | Meaning |
|---|---|---|
| `WAREHOUSE_CREDENTIALS_KEYS` | unset | Fernet keys, comma-separated, newest first, that encrypt stored warehouse credentials. While unset, storing or using a credential answers `503 credentials_unavailable`. In staging and production a malformed value is refused when the API starts, and the full profile's modules do not load until it is fixed. |
| `WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS` | 1800 | The highest `query_timeout_seconds` a connection may set. |
| `WAREHOUSE_MAX_BYTES_PER_QUERY` | 214748364800 (200 GiB) | The highest byte limit a connection may set. |
| `WAREHOUSE_MAX_RUNS_PER_DAY` | 200 | The highest `max_runs_per_day` a connection may set. |
| `WAREHOUSE_MAX_CONCURRENT_JOBS` | 4 | Threads per API process for warehouse calls. A call that finds none free is refused at once with `429 warehouse_busy`; it never waits. |
| `WAREHOUSE_MAX_CONCURRENT_RUNS` | 2 | Analyses and previews in flight across the deployment. |

A stored credential that none of the configured keys can open answers
`409 credentials_undecryptable` and the connection shows
`needs_new_credentials`; an admin replaces it. Deleting a connection deletes
its row and its credentials; its sources go with it, and its past analyses
keep their results and the connection's name.

## API

All routes are under `/api/v1/warehouse/analysis`, need a signed-in user, and
take credentials only in a request body.

| Method and path | What it does |
|---|---|
| `GET /connectors` | The warehouses this code knows and whether each is available here |
| `GET`, `POST /connections` | List connections; create one |
| `GET`, `PUT`, `DELETE /connections/{connection_id}` | Read, change or delete a connection |
| `POST /connections/{connection_id}/test` | Sign in and run a statement that reads no table |
| `POST /connections/test` | Test a connection's settings before saving them (nothing is stored) |
| `POST /connections/{connection_id}/regenerate-key` | Generate a new key pair, for warehouses whose key the platform generates |
| `GET`, `POST /sources` | List sources; create one |
| `GET`, `PUT`, `DELETE /sources/{source_id}` | Read, change or delete a source |
| `POST /sources/{source_id}/validate` | Check the table and columns against the warehouse's metadata |
| `POST /sources/{source_id}/preview` | Counts and time bounds for the last 7 days, or a window you give |
| `POST /experiments/{experiment_id}/runs` | Start an analysis (answers 202 with the run id) |
| `GET /experiments/{experiment_id}/runs` | The experiment's analyses, newest first |
| `GET /runs/{run_id}` | One analysis or preview: its status, results and, for ANALYST and above, the SQL sent |

A source names a table or view and maps its columns:

```json
{
  "kind": "metric",
  "connection_id": "3f6c1a52-0d7e-4a55-9a51-2b1f3f0b6e10",
  "name": "Purchases",
  "table": "ANALYTICS.PUBLIC.PURCHASES",
  "columns": {"unit_id": "user_id", "event_at": "purchased_at"},
  "metric_type": "proportion",
  "conversion_window_hours": 168,
  "filters": [{"column": "status", "operator": "eq", "value": "paid"}]
}
```

Each part of the table name and each column must match the warehouse's
identifier rules, and a filter's text value may contain only letters, digits,
spaces and `_ . : @ / + -`; anything else is refused with 422, never escaped.
A source must be validated before a preview or an analysis uses it, and
editing it clears its validation.

An analysis names the sources, and optionally the window and which warehouse
variant label is which experiment variant (labels not listed are matched to a
variant by its name):

```json
{
  "connection_id": "3f6c1a52-0d7e-4a55-9a51-2b1f3f0b6e10",
  "assignment_source_id": "8a0d5c1e-7b1f-4a8e-9f0e-6a0b2c3d4e5f",
  "metric_source_ids": ["b2c7e9f1-3a4d-4c5b-8e6f-7a8b9c0d1e2f"],
  "window_start": "2026-09-01T00:00:00Z",
  "variant_map": {"ctl": "0f1e2d3c-4b5a-4968-8776-655443322110"}
}
```

The window defaults to the experiment's start date up to its end date or now;
a time without an offset is read as UTC. The run moves from `queued` to
`running` to `succeeded` or `failed`. A failed run carries an `error_code` from
a fixed set and reports no numbers; a metric that could not be computed says
`Not computed:` and why, never 0. A mean metric needs at least 2 units in
every variant (`Not computed: fewer than 2 units` otherwise). When the values
vary in neither the control nor a treatment, that treatment has no p-value and
its `note` says `Not computed: no variation`.

Errors are `{"detail": {"code": ..., "message": ...}}`. A request that fails
validation lists where and why, but never repeats the value you sent.

## If you ran an earlier full-profile release

As of v0.9.0, the earlier `/api/v1/warehouse` endpoints (saved connections,
sync, and the ClickHouse, MySQL and Databricks connectors) are removed, and the
migration to the warehouse analysis tables deletes the connections they saved
(see [Upgrading and migrations](../self-hosting/migrations.md)). Rotate every
credential that was ever saved there, and every credential sent to those endpoints in a
query parameter: it may be in your access logs.

See [Modules and profiles](../getting-started/modules.md) for what each
profile includes.
