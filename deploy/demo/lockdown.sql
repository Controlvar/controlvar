-- deploy/demo/lockdown.sql: after the seeds and before the API serves,
-- leave exactly one password that signs in: the demo account's.
--
--   psql -X -v ON_ERROR_STOP=1 -v demo_email=<DEMO_ACCOUNT_EMAIL> -f lockdown.sql
--
-- reset.sh runs it (lib.sh, demo_rebuild). It keeps every user ACTIVE: the
-- storefronts' and the simulators' API keys belong to admin@demo.com, and the
-- API refuses a key whose owner is inactive. Instead every other user's
-- password hash becomes '!', which no password matches (bcrypt rejects it as
-- malformed and the API counts that as a wrong password).
--
-- It then refuses to commit -- the whole transaction rolls back and psql exits
-- non-zero -- unless exactly one user still has a hash other than '!' and that
-- user is the demo account: active, not a superuser, role VIEWER. An unset
-- demo_email is a syntax error, an empty one matches nobody: both refuse.
\set ON_ERROR_STOP on

BEGIN;

SET LOCAL search_path TO experimentation;

-- Compared the way the API compares sign-in addresses: the database's lower().
SELECT set_config('controlvar_demo.email', lower(:'demo_email'), true);

-- IS DISTINCT FROM: a user with no email address is locked too.
UPDATE users
   SET hashed_password = '!'
 WHERE lower(email) IS DISTINCT FROM current_setting('controlvar_demo.email');

DO $$
DECLARE
    usable integer;
    demo_ok integer;
BEGIN
    SELECT count(*) INTO usable
      FROM users
     WHERE hashed_password IS DISTINCT FROM '!';

    SELECT count(*) INTO demo_ok
      FROM users
     WHERE hashed_password IS DISTINCT FROM '!'
       AND hashed_password IS NOT NULL
       AND lower(email) = current_setting('controlvar_demo.email')
       AND is_active IS TRUE
       AND is_superuser IS FALSE
       AND upper(role::text) = 'VIEWER';

    IF usable <> 1 OR demo_ok <> 1 THEN
        RAISE EXCEPTION
            'lockdown refused: % user(s) can still sign in; expected exactly one, the demo account %, active, not a superuser, role VIEWER',
            usable, current_setting('controlvar_demo.email');
    END IF;
END
$$;

COMMIT;
