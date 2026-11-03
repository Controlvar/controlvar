-- tests/scale/preload.sql: the history the scale harness loads straight into
-- the database of its own Compose stack (#1608).
--
-- This file defines one function and loads nothing by itself. The harness
-- (scripts/scale_stack.py fixtures) pipes it, followed by one call, to psql
-- inside the stack's postgres container. The integration test
-- (backend/tests/integration/test_scale_preload.py) runs the same file
-- through psycopg2. So it stays plain SQL that both accept unchanged: no psql
-- variables and no backslash commands.
--
--     SELECT pg_temp.scale_preload(schema, users, events, raw_metrics,
--                                  user_prefix, id_width);
--
-- The function is created in pg_temp, so it lasts only as long as the session
-- that created it and nothing is left behind in the database.
--
-- * It refuses to load anything unless the experiment scale_ab exists with
--   exactly two variants of different names. An installation the harness did
--   not set up normally has no scale_ab, so it is not loaded into by
--   accident.
-- * User i (0 <= i < users) is user_prefix followed by i, zero-padded to
--   id_width digits. It is assigned in scale_ab to variant (i mod 2) of the
--   two variants ordered by name in byte order (COLLATE "C", the order
--   Python's sorted() gives), so the harness knows each returning user's
--   variant without asking the server.
-- * Event j belongs to user (j mod users), and the events are spread evenly
--   over the 30 days before the call. The raw_metrics rows are flag
--   evaluations of the scale_flag_ flags, all older than 15 minutes.
-- * It loads once. A second call finds the users already assigned and loads
--   nothing; any other number of them already assigned is refused.
-- * It ends with ANALYZE, so the planner sees the volumes.

CREATE OR REPLACE FUNCTION pg_temp.scale_preload(
    target_schema text,
    users integer,
    events integer,
    raw_metrics integer,
    user_prefix text,
    id_width integer
) RETURNS text
LANGUAGE plpgsql
AS $preload$
DECLARE
    experiment uuid;
    variant_ids uuid[];
    variant_names bigint;
    flag_ids uuid[];
    already bigint;
    now_utc timestamp := date_trunc('second', now() AT TIME ZONE 'UTC');
BEGIN
    IF users IS NULL OR users < 1
       OR events IS NULL OR events < 0
       OR raw_metrics IS NULL OR raw_metrics < 0 THEN
        RAISE EXCEPTION 'scale_preload: users must be at least 1, events and '
            'raw_metrics at least 0';
    END IF;
    IF user_prefix IS NULL OR user_prefix = '' OR id_width IS NULL
       OR length((users - 1)::text) > id_width THEN
        RAISE EXCEPTION 'scale_preload: % users do not fit in % digits after a '
            'non-empty prefix', users, id_width;
    END IF;

    EXECUTE format('SELECT id FROM %I.experiments WHERE key = $1', target_schema)
        INTO experiment USING 'scale_ab';
    IF experiment IS NULL THEN
        RAISE EXCEPTION 'scale_preload: schema % has no experiment scale_ab. It '
            'loads only into a stack the scale harness set up (run its fixtures '
            'first).', target_schema;
    END IF;

    EXECUTE format(
        'SELECT array_agg(id ORDER BY name COLLATE "C"), count(DISTINCT name) '
        'FROM %I.variants WHERE experiment_id = $1', target_schema)
        INTO variant_ids, variant_names USING experiment;
    IF coalesce(array_length(variant_ids, 1), 0) <> 2 OR variant_names <> 2 THEN
        RAISE EXCEPTION 'scale_preload: scale_ab must have exactly two variants '
            'with different names, it has %',
            coalesce(array_length(variant_ids, 1), 0);
    END IF;

    EXECUTE format(
        'SELECT count(*) FROM %I.assignments WHERE experiment_id = $1 '
        'AND left(user_id, length($2)) = $2', target_schema)
        INTO already USING experiment, user_prefix;
    IF already = users THEN
        RETURN format('already loaded: %s users in scale_ab', users);
    ELSIF already <> 0 THEN
        RAISE EXCEPTION 'scale_preload: % of % users are already assigned in '
            'scale_ab. It loads once, into a fresh stack: take the stack down '
            'and up again.', already, users;
    END IF;

    EXECUTE format(
        'SELECT array_agg(id ORDER BY key COLLATE "C") FROM %I.feature_flags '
        'WHERE left(key, length($1)) = $1', target_schema)
        INTO flag_ids USING 'scale_flag_';
    IF raw_metrics > 0 AND coalesce(array_length(flag_ids, 1), 0) = 0 THEN
        RAISE EXCEPTION 'scale_preload: schema % has no scale_flag_ flags to '
            'record evaluations of (run the fixtures first)', target_schema;
    END IF;

    -- 2592000 seconds is 30 days.
    EXECUTE format(
        'INSERT INTO %I.assignments '
        '(id, experiment_id, variant_id, user_id, created_at, updated_at) '
        'SELECT gen_random_uuid(), $1, ($2)[mod(i, 2) + 1], '
        '$3 || lpad(i::text, $4, ''0''), t, t '
        'FROM generate_series(0, $5 - 1) AS i, '
        'LATERAL (SELECT $6 - make_interval(secs => '
        '(($5 - i)::bigint * 2592000 / $5)::double precision) AS t) AS at',
        target_schema)
        USING experiment, variant_ids, user_prefix, id_width, users, now_utc;

    IF events > 0 THEN
        EXECUTE format(
            'INSERT INTO %I.events '
            '(id, event_type, event_name, user_id, experiment_id, variant_id, '
            'created_at, updated_at) '
            'SELECT gen_random_uuid(), ''scale_conversion'', '
            '''scale_conversion'', $1 || lpad(mod(j, $2)::text, $3, ''0''), $4, '
            '($5)[mod(mod(j, $2), 2) + 1], '
            'to_char(t, ''YYYY-MM-DD"T"HH24:MI:SS"+00:00"''), t '
            'FROM generate_series(0, $6 - 1) AS j, '
            'LATERAL (SELECT $7 - make_interval(secs => '
            '(($6 - j)::bigint * 2592000 / $6)::double precision) AS t) AS at',
            target_schema)
            USING user_prefix, users, id_width, experiment, variant_ids, events,
                now_utc;
    END IF;

    IF raw_metrics > 0 THEN
        EXECUTE format(
            'INSERT INTO %I.raw_metrics '
            '(id, metric_type, "timestamp", feature_flag_id, user_id, value, '
            'count, created_at, updated_at) '
            'SELECT gen_random_uuid(), ''flag_evaluation'', t, '
            '($1)[mod(k, $2) + 1], $3 || lpad(mod(k, $4)::text, $5, ''0''), '
            '1, 1, t, t '
            'FROM generate_series(0, $6 - 1) AS k, '
            'LATERAL (SELECT $7 - interval ''15 minutes 1 second'' '
            '- make_interval(secs => '
            '(($6 - k)::bigint * 2592000 / $6)::double precision) AS t) AS at',
            target_schema)
            USING flag_ids, array_length(flag_ids, 1), user_prefix, users,
                id_width, raw_metrics, now_utc;
    END IF;

    EXECUTE format('ANALYZE %I.assignments', target_schema);
    EXECUTE format('ANALYZE %I.events', target_schema);
    EXECUTE format('ANALYZE %I.raw_metrics', target_schema);

    RETURN format('loaded %s users, %s events and %s raw_metrics rows into %s',
        users, events, raw_metrics, target_schema);
END
$preload$;
