# Logging Standards

Logging is **structlog**, configured once in `backend/app/core/logger.py`.
In production every event renders as a single-line JSON object, which is what
CloudWatch Logs Insights expects; in development it renders as colourised text
through structlog's `dev` renderer. The switch is `json_logs`.

## Getting a logger

```python
from backend.app.core.logger import get_logger

logger = get_logger(__name__)
```

Pass structured data as **keyword arguments**, not string interpolation and
not `extra={...}`:

```python
logger.info("experiment assigned", experiment_key=key, variant=variant.name)
logger.warning("cache miss", cache="evaluation", key=cache_key)
logger.error("failed to reach the warehouse", warehouse="snowflake", exc_info=True)
```

Every keyword becomes its own JSON field, so `variant="control"` is queryable
in Logs Insights. Interpolating it into the message (`f"variant {variant}"`)
buries it in a string and is not.

## Fields on every line

Added by the shared processor chain, without the caller doing anything:

| field | source |
|---|---|
| `timestamp` | `TimeStamper(fmt="iso")` |
| `level` | `add_log_level` |
| `event` | the message — structlog's name for it, not `message` |
| `logger` | `add_logger_name` |
| `service` | `_add_service`, the configured service name |
| `exception` | `format_exc_info`, only in JSON mode and only when `exc_info` is set |

## Per-request fields

`RequestIDMiddleware` binds these to the async context, so **every** log line
emitted while handling a request carries them, with no bound logger passed
around:

| field | value |
|---|---|
| `request_id` | also returned to the client as the `X-Request-ID` header |
| `path` | the request path |
| `method` | the HTTP method |

To add your own for the rest of a request:

```python
from backend.app.core.logger import bind_log_context

bind_log_context(user_id=str(user.id), workspace_id=str(ws.id))
```

`request_id` is the field to correlate on. It appears on every line of a
request and is handed back to the caller in the response header, so a user
reporting an error can quote it.

## Levels

| level | use |
|---|---|
| `DEBUG` | detail for local debugging; not enabled in production |
| `INFO` | normal operational events — an assignment made, a rollout stage advanced |
| `WARNING` | something recoverable that may need attention — a cache miss on a hot path, a retry |
| `ERROR` | an operation failed and a user saw it; include `exc_info=True` |
| `CRITICAL` | the service cannot continue |

## What must never be logged

No credentials, API keys, tokens, password hashes, or PHI.

**Nothing masks a log line.** `backend/app/utils/masking.py` blanks
`password`, `token`, `api_key`, `secret`, `credential`, `authorization`,
`access_token`, `refresh_token`, `private_key` and friends, and partially
masks emails, phone numbers, credit card numbers and IP addresses. Only
`LoggingMiddleware` (`backend/app/middleware/logging_middleware.py`) calls it,
and the application does not mount that middleware.

A keyword you pass to `logger.info(...)` lands in CloudWatch verbatim. So log
the identifier, not the object. A `User` or a tracking payload carries an email
address or an attribute you did not mean to persist.

## What flag evaluation and assignment log

The `user_id` an SDK sends is your end user's identifier, and it is often an
email address. Evaluation and assignment run on every SDK request, so they
log none of it:

- **No line carries the end user's id or attribute values, at any level.** This
  covers `AssignmentService`, `FeatureFlagService` evaluation, the rules
  engine and its evaluation cache, `EventService`'s writes (exposures and
  tracked events), and the flag-evaluation and event-processor code under
  `backend/lambda/`, which no stack deploys, including their shared DynamoDB
  and Kinesis helpers.
- **Routine per-request lines are `DEBUG`**: an assignment made, a sticky
  assignment reused, a user left out by holdout, mutual exclusion or targeting,
  a flag evaluated. They name the experiment or flag and the variant, not the
  user. At the production level (`INFO`) a busy installation writes none of
  them.
- **An error on those paths logs the exception's type (for an AWS error, with
  its error code), not its text.** An
  exception's text can repeat a value, and a database error repeats the row's
  parameters, the user id among them. A flag evaluation error's full text
  still goes to the `error_logs` table, where the safety monitor reads it.
- A reassignment, which an operator requests, logs one `INFO` line naming the
  experiment and the variant, not the user.

To follow one user through the system, use the data that is kept for it: the
assignments and events tables, and `error_logs`.

No middleware binds the dashboard user's id to a log line; the
[audit log](api/audit-logging.md) records who changed what.
