# HIPAA Compliance Overview

!!! info "Part of the `hipaa` module"
    HIPAA is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

## Summary

The `hipaa` module keeps Business Associate Agreement (BAA) records and a PHI access log, encrypts and decrypts single values with a key you configure, and reports a readiness status.

## Components

### 1. PHI Encryption (`modules/backend/app/core/phi_encryption.py`)

Symmetric encryption for Protected Health Information (PHI) using Fernet (AES-128-CBC + HMAC-SHA256).

**Key generation:**
```python
from modules.backend.app.core.phi_encryption import PHIEncryption
key = PHIEncryption.generate_key()  # Store this in AWS Secrets Manager
```

**Usage:**
```python
enc = PHIEncryption()  # Reads key from settings.PHI_ENCRYPTION_KEY
ciphertext = enc.encrypt("John Smith DOB 1990-01-01")
plaintext  = enc.decrypt(ciphertext)
```

**Properties:**
- Each `encrypt()` call uses a random IV — same plaintext produces different ciphertexts
- HMAC authentication prevents tampered ciphertext from decrypting
- `decrypt()` raises `ValueError` on any integrity failure
- Encryption uses one key, `PHI_ENCRYPTION_KEY`. A value encrypted under another key does not decrypt: `/decrypt` answers `400`, so keep every key you have used.

### 2. PHI Audit Log (`modules/backend/app/models/phi_audit_log.py`)

Entries in `experimentation.phi_audit_logs` are written by two routes: `POST /audit-logs`, which records the access you send, and `POST /decrypt`, which records a `READ` by the calling user. Each entry has:
- Who accessed (user_id FK to users)
- What was accessed (resource_type, resource_id)
- What action (READ / WRITE / DELETE / EXPORT)
- Which fields (phi_fields_accessed — HIPAA minimum-necessary principle)
- Why (purpose: treatment / operations / research)
- Network context (ip_address, user_agent)
- Retention marker (retention_years = 6, per HIPAA §164.530(j))

### 3. BAA Configuration (`modules/backend/app/models/baa_config.py`)

Business Associate Agreements are tracked in `experimentation.baa_configs`. Each record stores:
- Covered entity contact (organization_name, signatory_name, signatory_email)
- Agreement period (effective_date, expiry_date)
- Data residency region (e.g., us-east-1)
- PHI categories covered (demographics, diagnosis, treatment, billing)
- A hash of the signed BAA document, as you send it (`signed_document_hash`)

### 4. API Endpoints (`modules/backend/app/api/v1/endpoints/hipaa.py`)

All endpoints at `/api/v1/hipaa` require ADMIN role (except `/encrypt` which also allows DEVELOPER).

| Method | Path | Description |
|--------|------|-------------|
| POST | `/baa` | Create BAA configuration |
| GET | `/baa` | List BAA configurations |
| GET | `/baa/{id}` | Get specific BAA |
| DELETE | `/baa/{id}` | Deactivate BAA (soft delete) |
| GET | `/audit-logs` | Paginated PHI audit log |
| POST | `/audit-logs` | Manually log PHI access |
| GET | `/report` | HIPAA compliance report |
| POST | `/encrypt` | Encrypt a PHI value |
| POST | `/decrypt` | Decrypt a PHI value |
| GET | `/data-residency` | Data residency configuration |
| GET | `/status` | HIPAA readiness status |

## Configuration

| Setting | What it does |
|---------|--------------|
| `PHI_ENCRYPTION_KEY` | The key `/encrypt` and `/decrypt` use. Without it they answer `503`. |
| `HIPAA_ENABLED` | Returned as `hipaa_enabled` by `/data-residency`. The routes work the same either way. |
| `HIPAA_ALLOWED_REGIONS` | Returned by `/data-residency` beside the deployment's region (default `["us-east-1","us-west-2"]`). |
| `HIPAA_AUDIT_LOG_RETENTION_YEARS` | Written into each new audit log entry as `retention_years` (default 6). |

## Readiness Status

`GET /api/v1/hipaa/status` returns:

```json
{
  "has_active_baa": true,
  "phi_encryption_configured": true,
  "audit_logging_enabled": true,
  "data_residency_configured": true,
  "overall_hipaa_ready": true
}
```

`overall_hipaa_ready` is true when an active BAA exists, `PHI_ENCRYPTION_KEY` is set and `HIPAA_ALLOWED_REGIONS` is not empty. It reports configuration; it does not inspect stored data or logs.

## Security Notes

- Audit logs are append-only — no DELETE endpoint is exposed
- BAA deactivation is a soft delete — records are preserved for audit history
