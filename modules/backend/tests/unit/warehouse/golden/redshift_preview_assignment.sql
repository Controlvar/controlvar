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
per_variant AS (
  SELECT variant, COUNT(DISTINCT unit_id) AS units
  FROM exposures
  WHERE variant IS NOT NULL
  GROUP BY variant
  ORDER BY units DESC, variant
  LIMIT 51
),
totals AS (
  SELECT COUNT(*) AS total_rows,
    COUNT(CASE WHEN unit_id IS NULL THEN 1 END) AS null_unit_rows,
    COUNT(CASE WHEN variant IS NULL THEN 1 END) AS null_variant_rows,
    MIN(CAST(FLOOR(EXTRACT(EPOCH FROM exposed_at)) AS BIGINT)) AS earliest,
    MAX(CAST(FLOOR(EXTRACT(EPOCH FROM exposed_at)) AS BIGINT)) AS latest
  FROM exposures
)
SELECT t.total_rows AS total_rows, t.null_unit_rows AS null_unit_rows,
  t.null_variant_rows AS null_variant_rows, t.earliest AS earliest,
  t.latest AS latest, v.variant AS variant, v.units AS units
FROM totals AS t
LEFT JOIN per_variant AS v ON 1 = 1
ORDER BY v.units DESC, v.variant
LIMIT 51
