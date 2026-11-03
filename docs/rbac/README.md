# Role-Based Access Control (RBAC) System

## Overview

Every deployment decides what a signed-in user may do from four roles and a separate superuser flag. This page explains them. In the full profile, custom roles and direct grants, the `rbac` module's, can add permissions to a user's role, never remove any: see the [RBAC API page](../api/rbac.md). Statements below about what a role cannot do describe the role alone.

## User Roles

Users in the system can have one of the following roles:

- **Admin**: Can create and manage experiments, and create and manage any feature flag
- **Developer**: Can create and manage experiments, and create and manage any feature flag
- **Analyst**: Can view experiments and feature flags, and create analytics reports
- **Viewer**: Can view experiments and every feature flag, and change neither

Every role, Viewer included, can create an API key for itself, and an API key writes
tracking data: assignments and events. See [API Key Management](../security/api-keys.md).

Feature flags are governed by permission, not by who created them: an owner is recorded on
each flag and shown, but owning a flag does not let an Analyst or Viewer change it.

Each role has a specific set of permissions that determine what actions they can perform on various resources.

**The Admin role is not a superuser.** Managing user accounts (`/api/v1/admin/users`),
global safety settings, and a flag's per-flag safety config (error-rate and latency
thresholds, rollback percentage) and its rollback are gated on the separate `is_superuser`
account flag, not on a role: an Admin without it is refused these the same as any other role.
The dashboard's Users, API Keys, Safety settings and Roles pages also require a superuser.
Through the API, the Admin role alone may list and delete other users' API keys
(`GET /api/v1/api-keys?all=true`, `DELETE /api/v1/api-keys/{id}`) and manage custom Roles;
only the dashboard pages for those need the superuser flag.

## Resources and Actions

Permissions are defined as combinations of resources and actions:

### Resource types

- **Experiment**: A/B tests and experiments
- **FeatureFlag**: Feature flags and toggles
- **User**: User accounts
- **Role**: User roles
- **Permission**: Permission assignments
- **Report**: Analytics reports and dashboards
- **APIKey**: API keys (who may list or delete other users' keys)

### Actions

- **CREATE**: Create new resources
- **READ**: View existing resources
- **UPDATE**: Modify existing resources
- **DELETE**: Remove resources
- **LIST**: Retrieve lists of resources

## Permission Matrix

The following table shows which roles can perform which actions on which resources:

| Role      | Experiment                 | Feature Flag               | User          | Report                    |
|-----------|----------------------------|----------------------------|---------------|---------------------------|
| Admin     | Create, Read, Update, Delete | Create, Read, Update, Delete | Create, Read, Update, Delete | Create, Read, Update, Delete |
| Developer | Create, Read, Update, Delete | Create, Read, Update, Delete | Read         | Read                      |
| Analyst   | Read                       | Read                       | Read         | Create, Read, Update, Delete |
| Viewer    | Read                       | Read                       | Read         | Read                      |

The User column is `ROLE_PERMISSIONS`' entry for the `User` resource
(`backend/app/core/permissions.py`); it is not what the dashboard or the
`/api/v1/admin/*` routes enforce. Those routes -- list, read, update, delete
and deactivate a user, and the dashboard's Users page -- depend on
`deps.get_current_superuser` directly and ignore this table entirely: an Admin
without `is_superuser` gets a 403 from every one of them, the same as every
other role.

## Ownership-Based Access

A user who creates a resource is recorded as its owner. What that record decides
depends on the resource.

**Feature flags.** A flag's owner is recorded and shown, but access to flags is decided
by permission, never by ownership: ADMIN and DEVELOPER may change any flag, and ANALYST and
VIEWER may change none by their role, including one they own. A custom role or direct grant
(full profile) of `feature_flag (update)` or `(delete)` admits any flag, owned or not.

**Experiments.** Under `/api/v1/experiments/{experiment_id}`:

- Reading an experiment (`GET /{experiment_id}`) and its results (`/results`,
  `/daily-results`, `/segmented-results/{segment_by}`) needs the role's READ on
  experiments. Who owns the experiment is not considered.
- Changing one -- `PUT /{experiment_id}`, `schedule`, `start`, `pause`, `complete`,
  `archive` and `metadata` -- needs READ and UPDATE on experiments. Who owns the
  experiment is not considered, so ANALYST and VIEWER change no experiment by their role,
  including one they own; a custom role or direct grant (full profile) of
  `experiment (update)` lets them change any.
- Deleting one (`DELETE /{experiment_id}`) needs READ and DELETE on experiments, and the
  experiment must be in DRAFT. Who owns the experiment is not considered: ADMIN and
  DEVELOPER may delete any DRAFT experiment, and so may anyone whose custom role or direct
  grant (full profile) gives `experiment (delete)`.
- A clone (`POST /{experiment_id}/clone`) needs READ on the source and CREATE on
  experiments, which a custom role or direct grant (full profile) of `experiment (create)`
  also gives.

A superuser passes every one of these checks.
