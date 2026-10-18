# Security Documentation

This directory contains the security compliance documentation for Controlvar.

| Document | Description |
|----------|-------------|
| [Security model](model.md) | What the platform does to protect a deployment |
| [Responding to an incident on your deployment](incident-response.md) | Where to report a vulnerability, and how to roll back |
| [Hardening Changes](hardening-changes.md) | Security controls implemented |

## Quick Reference

- Report a security issue: privately, as [SECURITY.md](https://github.com/Controlvar/controlvar/blob/main/SECURITY.md) describes
- An incident on your deployment: [Responding to an incident on your deployment](incident-response.md)

## Key Security Controls (Implemented)

- Security headers: HSTS (1yr + preload), CSP (`default-src 'none'`, except the two API reference pages, which allow the jsdelivr assets they load), X-Frame-Options: DENY — `backend/app/middleware/security_middleware.py`
- RBAC enforcement: 4 roles (ADMIN, DEVELOPER, ANALYST, VIEWER) — `backend/app/core/permissions.py`
- bcrypt password hashing — `backend/app/core/security.py`
- API key expiry and revocation (an expired or revoked key is refused) — `backend/app/models/api_key.py`, `backend/app/api/deps.py`
- Safety auto-rollback for feature flags — `backend/app/core/safety_scheduler.py`
