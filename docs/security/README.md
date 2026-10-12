# Security Documentation

This directory contains the security compliance documentation for Experimently.

| Document | Description |
|----------|-------------|
| [Responding to an incident on your deployment](incident-response.md) | Where to report a vulnerability, and how to roll back |
| [Hardening Changes](hardening-changes.md) | Security controls implemented |

## Quick Reference

- Report a security issue: privately, as [SECURITY.md](https://github.com/getexperimently/experimently/blob/main/SECURITY.md) describes
- An incident on your deployment: [Responding to an incident on your deployment](incident-response.md)
- Last audit date: March 2026
- Next review: June 2026

## Key Security Controls (Implemented)

- Security headers: HSTS (1yr + preload), CSP (`default-src 'none'`, except the two API reference pages, which allow the jsdelivr assets they load), X-Frame-Options: DENY — `backend/app/middleware/security_middleware.py`
- RBAC enforcement: 4 roles (ADMIN, DEVELOPER, ANALYST, VIEWER) — `backend/app/core/permissions.py`
- bcrypt password hashing — `backend/app/core/security.py`
- API key expiry and revocation (an expired or revoked key is refused) — `backend/app/models/api_key.py`, `backend/app/api/deps.py`
- Audit logging for all CRUD operations — `backend/app/models/audit_log.py`
- Safety auto-rollback for feature flags — `backend/app/core/safety_scheduler.py`
- Structured logging with error sanitization — `backend/app/middleware/`

## Critical Items Before Production Launch

1. Replace `decode_token` stub with real Cognito JWT validation (`backend/app/core/security.py`)
2. Override weak default credentials (`FIRST_SUPERUSER_PASSWORD = "admin"` in `config.py`)
3. ~~Hash API keys at rest~~ Done: `api_keys.key` holds only the SHA-256 hash of each key (`hash_api_key` in `backend/app/core/security.py`); the plaintext is shown once, at creation
4. Add rate limiting to `/api/v1/auth/token` login endpoint
5. Reduce JWT expiry from 8 days to 1 hour with refresh token rotation
6. Enable AWS WAF with OWASP Core Rule Set
7. Enable AWS GuardDuty in all regions
