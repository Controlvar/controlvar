# Data Export & Reporting API (EP-020)

This document describes the data export and reporting endpoints available under `/api/v1/export/`.

---

## Authentication

Every export endpoint takes a user's access token, from `POST /api/v1/auth/login`, in the
`Authorization: Bearer` header. Every role (ADMIN, DEVELOPER, ANALYST, VIEWER) may use
them. Without a token they answer `401 Unauthorized`.

The result columns (each variant's assignments, conversions, rate, p-value and
significance, and each experiment's winner and recommendation) are the numbers
`GET /api/v1/results/{experiment_id}` reports for the experiment's primary metric with no
options: the experiment's stored `correction_method` and `confidence_level`
(Benjamini-Hochberg at 0.95 unless it was created with others; see
[How results are judged](endpoints.md#how-results-are-judged)). The export takes no such
parameters. Asked with another `correction_method` or `confidence_level`, `/results` can
report a different `is_significant`, winner and recommendation than the export; the
counts, rates and unadjusted p-values are the same.

`p_value` is the unadjusted p-value, while `is_significant` is decided on the corrected
one. With two or more treatments a row can therefore show `p_value` 0.035 and
`is_significant` false: under Benjamini-Hochberg that treatment's adjusted p-value can be
0.071. The export has no adjusted p-value column; `/results` reports it as
`adjusted_p_value`.

**Empty in this release:** `experiments_with_winners` in the overview is `0`
([#244](https://github.com/Controlvar/controlvar/issues/244)).

Run the commands on this page in one terminal, in order, against the stack from the
[Quick Start](../getting-started/quick-start.md). Each uses the shell variables set by the
ones before it. Log in first:

```{.bash exec}
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@demo.com","password":"Demo1234!"}' | jq -r .access_token)

curl -s localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN" | jq .role
```
<!-- expect: "ADMIN" -->

It prints `"ADMIN"`. The downloads below are written to the current directory, so this
moves to a new, empty one first:

```{.bash exec}
cd "$(mktemp -d)"
ls | wc -l
```
<!-- expect: 0 -->

It prints `0`.

---

## Rate Limiting

Export endpoints allow 10 requests a minute per client address, shared by all export
endpoints, because each export computes results for every experiment it covers. The
count is shared: 4 calls to `/experiments`, 3 to `/variants` and 3 to
`/reports/experiments/{id}` (for any ids) use up the minute. Above it every export
endpoint answers `429 Too Many Requests` with a `Retry-After: 60` header; the rest of
the API keeps its own limits.
For bulk data pipelines, consider exporting once and caching the result.

---

## Export Endpoints

The export URLs have no trailing slash: with one, the API answers `307`, which `curl`
doesn't follow.

### GET /api/v1/export/experiments

Download all experiments as a CSV (default) or JSON file.

#### Query Parameters

| Parameter    | Type     | Default     | Description                                                  |
|--------------|----------|-------------|--------------------------------------------------------------|
| `format`     | `string` | `csv`       | Output format. Accepted values: `csv`, `json`                |
| `scope`      | `string` | `summary`   | Data scope. Only `summary` is available: `events` and `assignments` answer `422` |
| `start_date` | `datetime` | `null`    | Inclusive start filter on `created_at` (ISO 8601, UTC)       |
| `end_date`   | `datetime` | `null`    | Inclusive end filter on `created_at` (ISO 8601, UTC)         |

#### CSV Column Names

| Column                  | Type      | Description                              |
|-------------------------|-----------|------------------------------------------|
| `experiment_id`         | string    | UUID of the experiment                   |
| `experiment_name`       | string    | Human-readable experiment name           |
| `status`                | string    | Current status (draft, active, completed, etc.) |
| `experiment_type`       | string    | Type: `a_b`, `mv`, `split_url`, `bandit` |
| `start_date`            | string    | ISO 8601 start date, or empty            |
| `end_date`              | string    | ISO 8601 end date, or empty              |
| `duration_days`         | float     | Days between start and end, or empty     |
| `total_assignments`     | integer   | Total user assignments to this experiment |
| `total_events`          | integer   | Total events tracked for this experiment |
| `winner_variant`        | string    | Name of the winning variant, as `/results` reports it, or empty |
| `recommendation`        | string    | `SHIP_VARIANT`, `KEEP_CONTROL`, `CONTINUE_TESTING` or `INCONCLUSIVE` (the sample-ratio check failed), as `/results` reports it; empty when the experiment's results cannot be computed (it has no control variant) |

#### Example curl

This downloads the CSV, which has a header row and one row per experiment.
`start_date` and `end_date` filter on when the experiment was created; this asks for the
ones created since the start of 2024:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/export/experiments?start_date=2024-01-01T00:00:00Z" -o experiments.csv

sed -n 1p experiments.csv
grep 'Checkout Button Color' experiments.csv | cut -d, -f3,4,10,11
```
<!-- expect: experiment_id,experiment_name,status,experiment_type,start_date,end_date,duration_days,total_assignments,total_events,winner_variant,recommendation -->
<!-- expect: active,a_b,green_button,SHIP_VARIANT -->

It prints the header row, then the demo data's `Checkout Button Color` experiment's
status, type, winner and recommendation, `active,a_b,green_button,SHIP_VARIANT`: on the
demo data the green button converts better, and the results API recommends shipping it.
The response carries the file name:

```text
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename=experiments_20260926_225646.csv
```

With `format=json`, the same rows are a JSON array; the variants example below asks
for JSON.

---

### GET /api/v1/export/variants

Download per-variant results for all (or filtered) experiments.

Each row represents one variant within one experiment. Its numbers are the variant's
result on the experiment's primary metric, as `GET /api/v1/results/{experiment_id}`
reports it. An experiment whose results cannot be computed (it has no control variant)
keeps its rows, with the result columns empty.

#### Query Parameters

Same as `/export/experiments` (`format`, `scope`, `start_date`, `end_date`).

#### CSV Column Names

| Column                      | Type    | Description                                         |
|-----------------------------|---------|-----------------------------------------------------|
| `experiment_id`             | string  | UUID of the parent experiment                       |
| `experiment_name`           | string  | Human-readable experiment name                      |
| `variant_id`                | string  | UUID of the variant                                 |
| `variant_name`              | string  | Variant name                                        |
| `is_control`                | boolean | Whether this is the control variant                 |
| `assignments`               | integer | Number of users assigned to this variant (`sample_size` in `/results`) |
| `conversions`               | integer | Number of assigned users with at least one conversion on the primary metric (a repeat purchaser counts once) |
| `conversion_rate`           | float   | Conversion rate [0, 1] (`mean` in `/results`)       |
| `p_value`                   | float   | Unadjusted p-value against control; empty for the control |
| `is_significant`            | boolean | Whether the result is significant after the experiment's correction, at its confidence level |
| `relative_improvement_pct`  | float   | Relative improvement over control (%); empty for the control |

Every result column (`assignments` to `relative_improvement_pct`) is empty for all of an
experiment's variants when its results cannot be computed (it has no control variant).

#### Example curl

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/export/variants?format=json" -o variants.json

jq -r '.[] | select(.experiment_name == "Checkout Button Color") | "\(.variant_name) \(.is_control)"' variants.json
```
<!-- expect: blue_button true -->
<!-- expect: green_button false -->

It prints the demo experiment's two variants, `blue_button` (the control) and
`green_button`. Their numbers are the ones the results API gives; this compares the two
for the demo experiment and prints `same`:

```{.bash exec}
EXP_ID=$(jq -r '[.[] | select(.experiment_name == "Checkout Button Color") | .experiment_id][0]' variants.json)

curl -s -H "Authorization: Bearer $TOKEN" "localhost:8000/api/v1/results/$EXP_ID?use_cache=false" \
  | jq -S '[.metrics[] | select(.is_primary) | .variants[] | {variant_name, assignments: .sample_size, conversions, conversion_rate: .mean, p_value}] | sort_by(.variant_name)' > from_results.json
jq -S --arg id "$EXP_ID" '[.[] | select(.experiment_id == $id) | {variant_name, assignments, conversions, conversion_rate, p_value}] | sort_by(.variant_name)' variants.json > from_export.json

cmp -s from_results.json from_export.json && echo same
```
<!-- expect: same -->

---

### GET /api/v1/export/feature-flags

Download feature flag usage data.

#### Query Parameters

| Parameter    | Type     | Default | Description                                                    |
|--------------|----------|---------|----------------------------------------------------------------|
| `format`     | `string` | `csv`   | Output format. Accepted values: `csv`, `json`                  |
| `start_date` | `datetime` | `null` | Inclusive start filter on `created_at` (ISO 8601, UTC)        |
| `end_date`   | `datetime` | `null` | Inclusive end filter on `created_at` (ISO 8601, UTC)          |

#### CSV Column Names

| Column                | Type    | Description                                          |
|-----------------------|---------|------------------------------------------------------|
| `flag_id`             | string  | UUID of the feature flag                             |
| `flag_key`            | string  | Machine-readable key (e.g. `checkout_v2`)            |
| `flag_name`           | string  | Human-readable name                                  |
| `status`              | string  | INACTIVE, ACTIVE, or ARCHIVED                        |
| `rollout_percentage`  | integer | Configured rollout percentage (0-100)                |
| `total_evaluations`   | integer | The number of metric records stored for the flag, which is not the number of times it was evaluated |
| `enabled_evaluations` | integer | Always `0` |
| `enabled_rate`        | float   | Always `0.0`, because `enabled_evaluations` is always `0` |
| `created_at`          | string  | ISO 8601 creation timestamp                          |
| `updated_at`          | string  | ISO 8601 last-updated timestamp                      |

#### Example curl

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:8000/api/v1/export/feature-flags -o feature_flags.csv

cut -d, -f2,4,5 feature_flags.csv | sort
```
<!-- expect: beta_features,ACTIVE,100 -->
<!-- expect: flag_key,status,rollout_percentage -->

It prints each flag's key, status and rollout percentage, among them the demo data's
`beta_features`, `ACTIVE` at `100`. There is no status filter: to keep only the active
flags, filter the file yourself.

---

## Report Endpoints

### GET /api/v1/export/reports/overview

Returns a JSON summary of platform-wide activity. Always returns `application/json`.

#### Query Parameters

| Parameter    | Type     | Default | Description                                              |
|--------------|----------|---------|----------------------------------------------------------|
| `start_date` | `datetime` | `null` | Report period start (inclusive, ISO 8601 UTC)           |
| `end_date`   | `datetime` | `null` | Report period end (inclusive, ISO 8601 UTC)             |

#### JSON Response Schema

```json
{
  "generated_at": "2024-03-15T14:30:00+00:00",
  "period_start": "2024-01-01T00:00:00+00:00",
  "period_end": "2024-03-15T23:59:59+00:00",
  "total_experiments": 42,
  "active_experiments": 7,
  "completed_experiments": 30,
  "total_feature_flags": 15,
  "active_feature_flags": 8,
  "total_assignments": 125000,
  "total_events": 340000,
  "experiments_with_winners": 12,
  "average_experiment_duration_days": 21.4
}
```

| Field                              | Type    | Description                                              |
|------------------------------------|---------|----------------------------------------------------------|
| `generated_at`                     | string  | ISO 8601 timestamp when the report was generated         |
| `period_start`                     | string  | Report period start, or null if no filter applied        |
| `period_end`                       | string  | Report period end, or null if no filter applied          |
| `total_experiments`                | integer | Total experiments in the platform (filtered by period)   |
| `active_experiments`               | integer | Experiments currently in ACTIVE status                   |
| `completed_experiments`            | integer | Experiments in COMPLETED status                          |
| `total_feature_flags`              | integer | Total feature flags                                      |
| `active_feature_flags`             | integer | Feature flags in ACTIVE status                           |
| `total_assignments`                | integer | Total user-experiment assignments                        |
| `total_events`                     | integer | Total tracking events                                    |
| `experiments_with_winners`         | integer | Counts saved reports that have a summary; nothing saves one in this release, so it is `0` |
| `average_experiment_duration_days` | float   | Average duration (days) of completed experiments, or null |

#### Example curl

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:8000/api/v1/export/reports/overview \
  | jq '{total_experiments, active_experiments, completed_experiments}'
```
<!-- expect: "total_experiments": 3 -->
<!-- expect: "active_experiments": 2 -->
<!-- expect: "completed_experiments": 1 -->

On the Quick Start's demo data it prints three experiments, two of them active and one
completed. With a period, only what was created in it is counted; nothing was created in
the first quarter of 2024:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/export/reports/overview?start_date=2024-01-01T00:00:00Z&end_date=2024-03-31T23:59:59Z" \
  | jq '{period_start, total_experiments}'
```
<!-- expect: "period_start": "2024-01-01T00:00:00+00:00" -->
<!-- expect: "total_experiments": 0 -->

It prints the period's start and `"total_experiments": 0`.

---

### GET /api/v1/export/reports/experiments/{experiment_id}

Returns a report for a single experiment. As JSON (the default) it has the experiment's
row from `/export/experiments` and its variant rows from `/export/variants`; with
`format=csv` it is the variant rows as CSV, with the same columns as `/export/variants`.
An id that no experiment has answers `404`, and one that is not a UUID answers `422`.

#### Path Parameters

| Parameter       | Type   | Description                    |
|-----------------|--------|--------------------------------|
| `experiment_id` | string | UUID of the experiment         |

#### Query Parameters

| Parameter | Type     | Default | Description                   |
|-----------|----------|---------|-------------------------------|
| `format`  | `string` | `json`  | `json`, or `csv` for the variant rows as a CSV file |

#### JSON Response Schema

```json
{
  "experiment_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "experiments": [
    {
      "experiment_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      "experiment_name": "Checkout Button Colour Test",
      "status": "completed",
      "experiment_type": "a_b",
      "start_date": "2024-01-01T00:00:00+00:00",
      "end_date": "2024-01-31T00:00:00+00:00",
      "duration_days": 30.0,
      "total_assignments": 10000,
      "total_events": 1100,
      "winner_variant": "Treatment",
      "recommendation": "SHIP_VARIANT"
    }
  ],
  "variants": [
    {
      "experiment_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      "experiment_name": "Checkout Button Colour Test",
      "variant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      "variant_name": "Control",
      "is_control": true,
      "assignments": 5000,
      "conversions": 500,
      "conversion_rate": 0.10,
      "p_value": null,
      "is_significant": false,
      "relative_improvement_pct": null
    }
  ]
}
```

#### Example curl

As JSON (the default) the report has the shape above: the experiment's row from
`/export/experiments` and its rows from `/export/variants`, which the examples above
already show. This asks for the demo experiment's report with `format=csv`, using the id
the variants example saved in `$EXP_ID`; the variant rows come as a CSV file:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/export/reports/experiments/$EXP_ID?format=csv" -o report.csv

sed -n 1p report.csv
cut -d, -f4,5 report.csv | sed 1d
```
<!-- expect: experiment_id,experiment_name,variant_id,variant_name,is_control,assignments,conversions,conversion_rate,p_value,is_significant,relative_improvement_pct -->
<!-- expect: blue_button,True -->
<!-- expect: green_button,False -->

It prints the header row, then each variant's name and whether it is the control.

---

## Error Responses

| Status Code | Description                                              |
|-------------|----------------------------------------------------------|
| `401`       | Missing or invalid Bearer token                          |
| `404`       | The report's experiment does not exist                   |
| `422`       | Invalid query parameter (e.g. `format=xml`), a `scope` other than `summary`, or a report id that is not a UUID |
| `429`       | Rate limit exceeded                                      |
| `500`       | Internal server error (check application logs)           |

### Example 422 Error

A `scope` other than `summary` is refused, with the reason in `detail`:

```{.bash exec}
curl -s -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/export/experiments?scope=events" | jq -r .detail
```
<!-- expect: scope=events is not supported; the export is available with scope=summary only -->

A value the parameter does not accept, such as `format=xml`, fails validation instead,
and the body lists each invalid parameter. It names the parameter and the rule, not the
value that was sent:

```json
{
  "detail": [
    {
      "type": "enum",
      "loc": ["query", "format"],
      "msg": "Input should be 'csv' or 'json'",
      "ctx": {"expected": "'csv' or 'json'"}
    }
  ]
}
```

---

## Notes

- Times are ISO 8601. `generated_at`, `period_start` and `period_end` carry a UTC offset; experiment `start_date` and `end_date`, and flag `created_at` and `updated_at`, are written as stored, without one.
- UUIDs are included in exports as plain strings (not resolved to names where the ID is already paired with a human-readable name in an adjacent column).
- Date filtering is **inclusive on both ends** (`created_at >= start_date AND created_at <= end_date`).
- CSV exports use Python's `csv.DictWriter` with `\r\n` line endings (RFC 4180).
- CSV cells are written spreadsheet-safe: a text value that begins with `=`, `+`, `-`,
  `@`, a tab or a carriage return is written with a single quote (`'`) in front of it, so
  a spreadsheet shows it as text. Numbers, and every value in the JSON exports, are
  written as they are.
- The `scope` parameter of the experiment and variant exports takes only `summary`;
  `events` and `assignments` are reserved and answer `422`.
