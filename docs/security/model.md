# Security model

What the API does to protect a deployment and its data, as the code does it today. To
report a vulnerability, follow
[SECURITY.md](https://github.com/Controlvar/controlvar/blob/main/SECURITY.md).

## Sign-in and passwords

- Passwords are stored as bcrypt hashes.
- `POST /api/v1/auth/login` and `POST /api/v1/auth/token` each take 10 requests a minute from one IP address; the next request in that minute is answered `429 Too Many Requests`.
- In `staging` and `production`, the API does not start with a `SECRET_KEY` shorter than 32 characters, or with a well-known `FIRST_SUPERUSER_PASSWORD` such as the demo one.

## Roles

- A signed-in user's role, `ADMIN`, `DEVELOPER`, `ANALYST` or `VIEWER`, and a separate superuser flag decide what the user may do ([RBAC](../rbac/README.md)).
- `ANALYST` and `VIEWER` cannot create, change or delete experiments or feature flags.
- Every role, `VIEWER` included, can create an API key for itself, and an API key writes tracking data: assignments and events.

## API keys

- An API key is stored only as a hash. Its value is shown once, in the response that creates it.
- A request that carries an API key and no sign-in is refused with `401` by the routes that manage experiments, feature flags and users.
- A deleted or expired key is refused with `401`.

## Response headers

- Every response except a CORS preflight carries `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`.
- The two API reference pages, `/api/v1/docs` and `/api/v1/redoc`, get a policy that also allows the scripts, styles and fonts they load.
- Outside development, every response except a CORS preflight also carries `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`.
