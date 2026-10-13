# EP-050: HIPAA Compliance Overview

!!! info "Part of the `hipaa` module"
    HIPAA is one of the optional modules -- present in the **full profile**, absent from the core one. A core deployment does not serve these routes. See [Modules and profiles](../getting-started/modules.md) for what each profile includes and how to run the full one.

## Summary

EP-050 adds HIPAA (Health Insurance Portability and Accountability Act) compliance controls to Experimently. The implementation covers the four primary HIPAA technical safeguard requirements: access controls, audit controls, integrity, and transmission security.

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

### 2. PHI Audit Log (`modules/backend/app/models/phi_audit_log.py`)

Every PHI access is recorded in `experimentation.phi_audit_logs` with:
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
- Document integrity hash (SHA-256 of signed BAA document)

### 4. HIPAA Service (`modules/backend/app/services/hipaa_service.py`)

Central business logic layer:

| Method | Description |
|--------|-------------|
| `log_phi_access(...)` | Create PHI audit log entry |
| `get_phi_audit_logs(...)` | Paginated query with filters |
| `create_baa(...)` | Create BAA configuration |
| `get_baa_configs(...)` | List BAA configurations |
| `deactivate_baa(...)` | Soft-delete a BAA |
| `generate_hipaa_report(...)` | Compliance summary report |
| `encrypt_phi_field(...)` | Encrypt a PHI value |
| `decrypt_phi_field(...)` | Decrypt a PHI value |
| `check_data_residency(...)` | Validate AWS region |
| `get_hipaa_status(...)` | Readiness checklist |

### 5. API Endpoints (`modules/backend/app/api/v1/endpoints/hipaa.py`)

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
| POST | `/decrypt` | Decrypt a PHI value (audit logged) |
| GET | `/data-residency` | Data residency configuration |
| GET | `/status` | HIPAA readiness status |

## Configuration

Add these settings to your environment or AWS Secrets Manager:

Required for PHI encryption:

```bash
PHI_ENCRYPTION_KEY=<generate with PHIEncryption.generate_key()>
```

Enable HIPAA mode:

```bash
HIPAA_ENABLED=true
```

Restrict to HIPAA-compliant AWS regions:

```bash
HIPAA_ALLOWED_REGIONS=["us-east-1","us-west-2"]
```

Audit log retention (HIPAA requires 6 years):

```bash
HIPAA_AUDIT_LOG_RETENTION_YEARS=6
```

## HIPAA Readiness Checklist

Use `GET /api/v1/hipaa/status` to verify all controls are in place:

```json
{
  "has_active_baa": true,
  "phi_encryption_configured": true,
  "audit_logging_enabled": true,
  "data_residency_configured": true,
  "overall_hipaa_ready": true
}
```

## Database Migration

The migration file is at:
`backend/app/db/migrations/versions/ep050_hipaa.py`

It creates two tables:
- `experimentation.phi_audit_logs`
- `experimentation.baa_configs`

**Do not run the migration** until:
1. A valid `PHI_ENCRYPTION_KEY` is in secrets management
2. A signed BAA has been obtained from each covered entity
3. Data residency has been confirmed for all PHI-processing regions

## Test Coverage

The Module Tests job of `pr-qa-gate.yml` runs these suites:

| File | Coverage |
|------|----------|
| `test_phi_encryption.py` | PHI encryption: key gen, encrypt/decrypt, edge cases |
| `test_hipaa_service.py` | Service layer: audit logs, BAA, report, data residency |
| `test_hipaa_api.py` | API: all 11 endpoints, RBAC, validation |

## Security Notes

- PHI encryption keys must be rotated periodically (Fernet supports key versioning via `MultiFernet`)
- Audit logs are append-only — no DELETE endpoint is exposed
- Decryption operations are automatically audit-logged
- BAA deactivation is a soft delete — records are preserved for audit history
- The `signed_document_hash` field (SHA-256) provides integrity verification for BAA documents
