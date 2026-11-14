WITH exposures AS (
  SELECT CAST("user_id" AS VARCHAR(65535)) AS unit_id,
    SUBSTR(CAST("variant" AS VARCHAR(65535)), 1, 64) AS variant,
    CAST("exposed_at" AS TIMESTAMPTZ) AS exposed_at
  FROM "analytics"."exposures"
  WHERE "experiment_key" = 'checkout-v2'
    AND CAST("exposed_at" AS TIMESTAMPTZ) >= CAST('2026-09-01 00:00:00+00:00' AS TIMESTAMPTZ)
    AND CAST("exposed_at" AS TIMESTAMPTZ) < CAST('2026-09-27 12:30:00+00:00' AS TIMESTAMPTZ)
    AND "platform" IN ('web', 'ios')
),
units AS (
  SELECT unit_id, MIN(variant) AS variant, MIN(exposed_at) AS first_exposed_at,
    COUNT(DISTINCT variant) AS n_variants
  FROM exposures
  WHERE unit_id IS NOT NULL AND variant IS NOT NULL
  GROUP BY unit_id
),
events AS (
  SELECT CAST("user_id" AS VARCHAR(65535)) AS unit_id, CAST("event_at" AS TIMESTAMPTZ) AS event_at, CAST("amount" AS DOUBLE PRECISION) AS metric_value
  FROM "analytics"."orders"
  WHERE CAST("event_at" AS TIMESTAMPTZ) >= CAST('2026-09-01 00:00:00+00:00' AS TIMESTAMPTZ)
    AND CAST("event_at" AS TIMESTAMPTZ) < CAST('2026-09-27 12:30:00+00:00' AS TIMESTAMPTZ)
    AND "status" = 'paid'
    AND "is_test" <> TRUE
    AND "amount" IS NOT NULL
),
per_unit AS (
  SELECT u.unit_id, u.variant, COUNT(e.event_at) AS n_events,
    COALESCE(SUM(e.metric_value), 0) AS y_raw
  FROM units AS u
  LEFT JOIN events AS e
    ON e.unit_id = u.unit_id
    AND e.event_at >= u.first_exposed_at
    AND e.event_at < LEAST(DATEADD(hour, 168, u.first_exposed_at), CAST('2026-09-27 12:30:00+00:00' AS TIMESTAMPTZ))
  WHERE u.n_variants = 1
  GROUP BY u.unit_id, u.variant
),
y AS (
  SELECT variant, CASE WHEN n_events > 0 THEN 1 ELSE 0 END AS converted,
    LEAST(CAST(y_raw AS DOUBLE PRECISION), CAST(500.0 AS DOUBLE PRECISION)) AS y
  FROM per_unit
),
k AS (
  SELECT AVG(y.y) AS k FROM y
),
diag AS (
  SELECT (SELECT COUNT(*) FROM events) AS metric_rows_in_window,
    (SELECT COUNT(*) FROM events AS e INNER JOIN units AS u ON e.unit_id = u.unit_id) AS metric_rows_matched,
    (SELECT COUNT(*) FROM events WHERE metric_value IS NULL) AS null_value_rows
)
SELECT y.variant AS variant,
  COUNT(*) AS n,
  SUM(y.converted) AS n_converted,
  CAST(MIN(k.k) AS VARCHAR) AS k,
  CAST(SUM(y.y - k.k) AS VARCHAR) AS sum_d,
  CAST(SUM((y.y - k.k) * (y.y - k.k)) AS VARCHAR) AS sum_d2,
  MIN(diag.metric_rows_in_window) AS metric_rows_in_window,
  MIN(diag.metric_rows_matched) AS metric_rows_matched,
  MIN(diag.null_value_rows) AS null_value_rows
FROM y CROSS JOIN k CROSS JOIN diag
GROUP BY y.variant
ORDER BY n DESC, variant
LIMIT 51
