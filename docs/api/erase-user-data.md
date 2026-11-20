# Erase one app user's data (beta)

Superuser only. `POST /api/v1/admin/app-users/erase` deletes everything Controlvar stores
under one `user_id` that your app sent. It does not delete dashboard accounts; that is
`DELETE /api/v1/admin/users/{user_id}`. Beta: the response may still change. There is no
undo. Restoring a backup brings back everything in it, not only this user.

## Before you erase

Stop sending the id first: rows your app sends during or after the erase are stored. An id
many visitors share (an SDK default such as `anonymous`) erases all of them. The id is
erased across every experiment and workspace of the installation. Because an erase cannot
be undone, the accounts that can run it should use
[two-step sign-in](../auth/two-factor.md).

## The request

Send the id in the body, signed in as a superuser with a bearer token; an API key is not
accepted:

```json
{"user_id": "user-to-erase"}
```

The id is matched exactly: case and spaces count, and `%` and `_` are plain characters. It
is 1 to 255 characters with no NUL, the ids the tracking routes store. Any other field is
refused with `422`.

A `200` answers what this call deleted, one count per table:

```json
{
  "receipt_id": "3f6d7a52-0b8e-4c1e-9a52-6f1f2b9d1c40",
  "complete": true,
  "ids_erased": 1,
  "deleted": {
    "events": 4, "raw_metrics": 2, "error_logs": 0, "llm_evaluations": 0,
    "event_dedup_keys": 1, "assignments": 1, "holdout_population": 1,
    "segment_members": 0, "feature_flag_overrides": 0
  }
}
```

An id with nothing stored answers `200` with every count 0. The response never holds the
id.

| Status | When |
|---|---|
| `200` | Erased, or nothing was stored under the id |
| `200` with `complete: false` | Time ran out, or a statement timed out: send the same request again |
| `401` | No token, an invalid token, or an API key alone |
| `403` | Not a superuser (`Not enough permissions`), or demo mode |
| `422` | The body or `user_id` is missing, empty, longer than 255 characters, not text, holds NUL or an unpaired surrogate, or the body has another field |
| `429` | More than 300 requests a minute to this path from one address |
| `500` | An unplanned failure. If the erase could not be recorded, nothing was erased; otherwise part of the data may already be erased: send the request again |

One call works for at most 20 seconds.

## What is deleted

Every row of these tables whose id column holds the id:

| Table | What it held |
|---|---|
| `events` | Events and exposures, with their metadata |
| `raw_metrics` | Flag evaluations and latencies |
| `error_logs` | Client errors, with their message, stack trace and attributes |
| `llm_evaluations` | LLM experiment evaluations: inputs, prompts and responses |
| `event_dedup_keys` | The `event_id` of each event sent with one |
| `assignments` | Experiment assignments, with the attributes sent |
| `holdout_population` | Global-holdout membership |
| `segment_members` | Membership of id-list segments |
| `feature_flag_overrides` | Per-user flag overrides |

A table that stores a `user_id` and is not in this list is a defect; report it.

## What is not deleted

- Aggregates, which hold no ids: analysis snapshots, bandit states and their history,
  aggregated flag metrics, balance-group counters, SDK evaluation counters, warehouse
  analysis runs, reports and safety rollback records.
- Results cached in Redis (see [Results afterwards](#results-afterwards)).
- Configuration that names the id: flag, experiment and segment rules.
- Dashboard accounts and their records: users, API keys, two-step sign-in, the audit log
  and the compliance trail, including older entries that mention the id.
- Rate-limit counters of requests whose path carries the id, for up to a minute.
- Copies outside the database: backups, snapshots and point-in-time recovery (a restore
  brings the rows back), load-balancer, API and database server logs, exports and reports already
  downloaded, your warehouse and what it feeds, DynamoDB counters and the event
  processor's items where they are deployed, and SDK caches on devices and servers.

## Results afterwards

Results are computed from the stored rows when they are read. After an erase the experiment
counts this user nowhere: their variant has one fewer user, without their exposures,
conversions or values. A p-value, a winner, an SRM check, CUPED, segment breakdowns,
Bayesian posteriors and the global-holdout comparison can change. A decision you already
made on the old numbers is not undone, and a sequential test that already stopped is not
revisited.

Results are cached for 5 minutes while an experiment runs and 24 hours otherwise; the daily
and segmented views for 1 hour. The erase does not clear them, so a completed experiment
can show the old numbers for up to 24 hours. `POST /api/v1/results/{experiment_id}/invalidate-cache`
clears one experiment's results at once; it is best-effort and answers `200` even when
Redis is unreachable. The daily and segmented views expire within the hour.

Today's analysis snapshot is replaced by the next fresh computation. Earlier days' snapshots
and the bandit-state history keep counting the user until
`ANALYSIS_HISTORY_RETENTION_DAYS` (default 90) purges them. Reports keep counting the user.

Aggregated flag metrics are recomputed for recent periods at the metrics scheduler's next
run; older periods keep counting the user.

A bandit's weights are recomputed at its next update (every
`BANDIT_UPDATE_INTERVAL_MINUTES`, default 5) without the user. The bandit reads PostgreSQL
first; DynamoDB counters are used only when PostgreSQL has no assignments for the experiment
or cannot be read, and then they still count the user. Users already routed keep their
assignments.

Warehouse-native analysis (full profile) reads your warehouse and still counts the user.

If your app sends the id again it is a new user: a new assignment (the same variant in a
plain experiment whose split has not changed; a bandit or balanced experiment may give
another), a new exposure and the same global-holdout arm. An `event_id` sent again is
stored again.

## The record

Each call writes two audit-log entries with the response's `receipt_id` as their
`entity_id`: `app_user_erase_started` when it starts, before anything is deleted, and
`app_user_erased` when it finishes, whose `new_value` is
`{"deleted": {...}, "ids_erased": 1, "complete": true}`. They hold counts only, never the
id. A start entry with no finish entry: the call did not finish; some rows may be gone, or
none. Send the request again. Find both with
`GET /api/v1/audit-logs/?entity_id=<receipt_id>`.

## When the answer is `complete: false`

Send the same request again until it is `true`. Each call's counts and entries cover that
call only.
