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
  SELECT unit_id, COUNT(DISTINCT variant) AS n_variants
  FROM exposures
  WHERE unit_id IS NOT NULL AND variant IS NOT NULL
  GROUP BY unit_id
)
SELECT (SELECT COUNT(*) FROM exposures) AS exposure_rows,
  (SELECT COUNT(*) FROM exposures WHERE unit_id IS NULL OR variant IS NULL) AS null_key_rows,
  (SELECT COUNT(*) FROM units) AS units,
  (SELECT COUNT(*) FROM units WHERE n_variants > 1) AS multi_variant_units,
  (SELECT COUNT(DISTINCT variant) FROM exposures WHERE unit_id IS NOT NULL AND variant IS NOT NULL) AS variant_values
