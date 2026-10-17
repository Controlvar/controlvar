# Environment Configuration Setup Guide

This guide explains how to set up the environment configuration for Controlvar.

## Overview

The API reads its settings with Pydantic Settings, in `backend/app/core/config.py`:

1. One `Settings` class, with the settings and their defaults
2. A class for each environment, `DevSettings`, `TestSettings` and `ProdSettings`, derived from it, that changes some of those defaults
3. An environment file for each environment, and one template, `.env.example`
4. The `ENVIRONMENT` variable, which chooses the class and the file

## Setup Steps

### 1. Create Environment Files

The settings class for each environment reads its own file from the directory you start the
application in (the repository root):

| `ENVIRONMENT` | File |
|---|---|
| `development` | `.env.dev` |
| `test` | `.env.test` |
| `staging`, `production` | `.env.prod` |

Variables set in the process environment override the file. `.env.example` at the
repository root lists the variables and is a starting point. Docker Compose reads a
different file, `.env` next to `docker-compose.yml`; see
[The Docker Compose stack](docker-compose-file.md).

### 2. Environment Variables

The most important environment variables to configure:

#### Database Connection
```dotenv
POSTGRES_SERVER=localhost
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=experimentation
POSTGRES_PORT=5432
POSTGRES_SCHEMA=experimentation
```

Choose your own `POSTGRES_PASSWORD` for anything but a local database.

#### Redis Connection
```dotenv
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
```

Set `REDIS_PASSWORD` as well if your Redis requires one; the local stack's does not.

#### Application Settings
```dotenv
PROJECT_NAME="Controlvar"
ENVIRONMENT=development
LOG_LEVEL=DEBUG
```

These are development values. In production, set `LOG_LEVEL` to `INFO`, `WARNING` or
`ERROR`.

#### Security Settings

`SECRET_KEY` signs login tokens. It must be a random string of at least 32 characters, and
staging and production refuse a shorter one or one of the placeholders in this repository.
Generate one:

```{.bash exec}
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
echo "${#SECRET_KEY}"
```
<!-- expect: 64 -->

It prints `64`, the key's length. Put `SECRET_KEY=` followed by the key (`echo "$SECRET_KEY"`
shows it) in your environment file, next to these:

```dotenv
CORS_ORIGINS=http://localhost:3100,http://localhost:3200,http://localhost:8000
SDK_RATE_LIMIT_PER_MINUTE=6000
```

- `CORS_ORIGINS` lists the browser origins, other than the API's own, that may call the
  API: a comma-separated list or a JSON array, such as
  `CORS_ORIGINS=https://shop.example.com,https://www.example.com`. `BACKEND_CORS_ORIGINS`
  is also accepted, in either form, and wins when both are set. Every origin in
  `DASHBOARD_ORIGINS` is added as well.
- When neither is set, development and test allow the local apps: `http://localhost`
  ports 3100, 3000, 3001, 3200 and 3300 (development's own default adds port 8000).
  Staging and production allow no other origin:
  the dashboard is served from the API's own origin and needs none. To call a staging or
  production API from a browser on another origin -- a site running the browser SDK, a
  demo app, or a local dashboard -- add that origin to `CORS_ORIGINS`.
- Credentials (cookies) are never allowed on a cross-origin request. `CORS_ORIGINS=*` in
  staging or production is accepted, allows every origin without credentials, and is
  reported with a WARNING at start-up; every staging or production start-up logs the
  effective list in one INFO line.
- `SDK_RATE_LIMIT_PER_MINUTE` is the per-IP ceiling for SDK traffic (`/api/v1/tracking/*`
  and flag evaluation). The default is 6000 a minute.

#### Background Jobs
```dotenv
BANDIT_UPDATE_INTERVAL_MINUTES=5
EXPERIMENT_SCHEDULER_INTERVAL_MINUTES=15
```

`BANDIT_UPDATE_INTERVAL_MINUTES` is how often multi-armed bandit weights are refreshed.
`EXPERIMENT_SCHEDULER_INTERVAL_MINUTES` is how often the experiment scheduler starts and
completes experiments whose scheduled `start_date` or `end_date` has passed. The default is
15; a value below 1 is refused when the settings load, so the API does not start.

### 3. Environment Selection

The application determines which environment to use based on the `ENVIRONMENT` variable:

- If it is not set, the API refuses to start and logs `ENVIRONMENT is not set: set it to
  development, test, staging or production before starting the API.`
- Valid values: `development`, `test`, `staging`, `production` (the older spellings `dev`
  and `prod` still work, with a deprecation warning)

Set it in your shell, in your deployment configuration, or in the environment file. In the
shell, for development:

```{.bash exec}
export ENVIRONMENT=development
```

For the others, give `test`, `staging` or `production` instead of `development`.

### 4. Production Configuration

For production deployment:

1. **Never commit production credentials to version control**
2. Use a secure method to provide the `.env.prod` file or environment variables
3. Ensure the following production values are properly set:
   - `SECRET_KEY` (generate a strong random string)
   - `POSTGRES_PASSWORD` (use a strong password)
   - `REDIS_PASSWORD` (if applicable)
   - `FIRST_SUPERUSER_PASSWORD` (for the admin user)
   - `LOG_LEVEL=WARNING` or `ERROR`

## Accessing Settings in Code

The settings are accessible throughout the application by importing the `settings` instance:

```python
from backend.app.core.config import settings

# Use settings
db_connection = settings.SQLALCHEMY_DATABASE_URI
app_name = settings.PROJECT_NAME
```

## Extending the Configuration

To add new configuration options:

1. Add the new setting to the `Settings` class in `backend/app/core/config.py`
2. Provide a default value
3. Override it in `DevSettings`, `TestSettings` or `ProdSettings` if one environment needs
   another default
4. Add it to `.env.example`

Example:
```python
# In backend/app/core/config.py
class Settings(BaseSettings):
    # ... existing settings
    
    # New setting
    CACHE_TTL_SECONDS: int = 300  # Default 5 minutes
```

## Troubleshooting

- If settings aren't loading correctly, check that your environment file exists and has the correct format
- Verify environment variables are set correctly
- Check file permissions on the .env files
- Set `LOG_LEVEL=DEBUG` to see more detailed logs
