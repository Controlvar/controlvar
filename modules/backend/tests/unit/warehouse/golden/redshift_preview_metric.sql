WITH events AS (
  SELECT CAST("user_id" AS VARCHAR(65535)) AS unit_id, CAST("event_at" AS TIMESTAMPTZ) AS event_at, CAST("amount" AS DOUBLE PRECISION) AS metric_value
  FROM "analytics"."orders"
  WHERE CAST("event_at" AS TIMESTAMPTZ) >= CAST('2026-09-01 00:00:00+00:00' AS TIMESTAMPTZ)
    AND CAST("event_at" AS TIMESTAMPTZ) < CAST('2026-09-27 12:30:00+00:00' AS TIMESTAMPTZ)
    AND "status" = 'paid'
    AND "is_test" <> TRUE
    AND "amount" IS NOT NULL
)
SELECT COUNT(*) AS total_rows,
  COUNT(CASE WHEN unit_id IS NULL THEN 1 END) AS null_unit_rows,
  COUNT(CASE WHEN metric_value IS NULL THEN 1 END) AS null_value_rows,
  MIN(CAST(FLOOR(EXTRACT(EPOCH FROM event_at)) AS BIGINT)) AS earliest,
  MAX(CAST(FLOOR(EXTRACT(EPOCH FROM event_at)) AS BIGINT)) AS latest
FROM events
