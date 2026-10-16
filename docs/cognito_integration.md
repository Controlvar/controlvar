# AWS Cognito Integration

This document explains how Controlvar integrates with AWS Cognito for authentication and role-based access control.

## Overview

The platform uses AWS Cognito for user authentication and leverages Cognito groups to automatically assign user roles within the application. This integration provides the following features:

1. The first time a Cognito user signs in, the platform creates their account and records
   their Cognito user ID (`sub`) on it. Later sign-ins find the account by that ID
   (see [How a sign-in finds its account](#how-a-sign-in-finds-its-account))
2. User roles are set from the Cognito groups in the user's access token
3. A change of Cognito groups is reflected in the user's role at their next access token
   (see [When a role change takes effect](#when-a-role-change-takes-effect))
4. Superusers (with full admin privileges) are automatically identified based on membership in designated admin groups

## Signing in under Cognito

The dashboard does not yet sign in with Cognito. Sign in through the API with
`POST /api/v1/auth/token`. SSO sign-in needs `AUTH_PROVIDER=local`.

Under `AUTH_PROVIDER=cognito` the dashboard's sign-in form answers that its endpoint is not
available, and an SSO sign-in does not sign the user in. Dashboard sign-in
under Cognito is tracked in [#708](https://github.com/Controlvar/controlvar/issues/708).

## Required settings

Under `AUTH_PROVIDER=cognito`, both `COGNITO_USER_POOL_ID` and `COGNITO_CLIENT_ID` are
required. A sign-in, and a call to `/api/v1/auth/me`, is accepted only for an access token
issued to this deployment's user pool (the one `COGNITO_USER_POOL_ID` names) and app client
(`COGNITO_CLIENT_ID`). An ID token, or an access token issued to a different app client, is
refused with a 401. With either setting unset, every sign-in is refused. Keep the pool ID in
the form Cognito gives it, `<region>_<id>` (for example `us-west-2_abcDEF123`): the token
issuer the platform expects is built from it, and a pool ID in any other form refuses every
sign-in. Each refused sign-in is logged at WARNING on the `backend.app.auth.cognito_sign_in`
logger with a `reason` field: `not_configured` when a setting is unset, `wrong_issuer` when
the token was not issued to the configured user pool and app client. The other reasons are
listed under [Reading a refused sign-in](#reading-a-refused-sign-in).

No IAM permission is needed for sign-in: the platform reads the user's groups from their
access token, not from the Cognito API.

## User pool requirements

The user pool that `COGNITO_USER_POOL_ID` names must require an email address:

- make `email` a **required** standard attribute of the pool;
- if users sign in through a federated identity provider (SAML, OIDC, Google and so on), map
  the provider's email claim to the pool's `email` attribute.

The platform creates a user's account the first time they sign in, and takes the account's
email address from the pool's `email` attribute. A first sign-in from a user with no email
address is refused, and no account is created.
Cognito does not let you change a pool's required attributes after the pool is created, so
set this when you create it. The reference pool in
`infrastructure/cdk/stacks/authentication_stack.py` already requires `email`.

## How a sign-in finds its account

Every request signed with a Cognito access token is matched to one account, in this order:

1. **The account linked to the Cognito user.** An account is linked when its
   `external_id` is `cognito:` followed by the user's Cognito user ID (`sub`). That account
   is used whatever its username, email address or password. A first sign-in links the
   account it creates; an administrator links an existing account (see
   [Linking an existing account](#linking-an-existing-account)).
2. **Otherwise, a new account**, created from the user's Cognito username, email address,
   given name and family name, with the role their groups give
   (see [Role Assignment Logic](#role-assignment-logic)). It has no password on the
   platform, and its `external_id` is set to `cognito:<sub>`.

The new account is not created, and the sign-in is refused, when:

- another account already has the user's Cognito username: an account with a password on
  the platform, an account linked to another identity, or an account with neither (for
  example one an earlier version of the platform created at a first Cognito sign-in). It
  is used only once an administrator links it;
- the user has no email address in the pool;
- another account already has the user's email address, in any letter case. An account is
  never linked by email address.

A refused sign-in answers every request with `401` and
`{"detail":"Could not validate credentials"}`, the same answer as an expired or invalid
token. The reason is in the API's log (see
[Reading a refused sign-in](#reading-a-refused-sign-in)).

An account's username, email address and name are never changed from Cognito after the
account is created.

To check which account a sign-in reaches, call `GET /api/v1/users/me` with the access token:
it answers with the account, or `401` when the sign-in is refused.
`GET /api/v1/auth/me` returns the user pool's record of the user and does not look at
accounts, so it answers `200` for a sign-in that is refused everywhere else.

## Self sign-up

| Variable Name | Description | Required | Default |
|---------------|-------------|----------|---------|
| `COGNITO_SELF_SIGNUP_ENABLED` | Under `AUTH_PROVIDER=cognito`, lets anyone who can reach the API register with `POST /api/v1/auth/signup` and `/confirm`. Off: both answer 404 and an administrator creates users ([Adding a user](#adding-a-user)). Ignored under `local`, where both always answer 404. The user pool must also allow self sign-up; the reference pool does not. A self-registered user is in no Cognito group, so their first sign-in creates an account with the Viewer role. | No | `false` |

`COGNITO_SELF_SIGNUP_ENABLED` takes `true` or `false`, in any letter case (`1`/`0`, `yes`/`no`,
`on`/`off`, `t`/`f` and `y`/`n` are read the same way). An empty value, or any other value,
stops the API from starting with an error naming the setting. The name must be in upper case: a lower-case
`cognito_self_signup_enabled` is ignored and the setting stays `false`.

**Upgrading from a release where sign-up was always on.** A user who signed up but never
confirmed before this release can no longer confirm through the API while the setting is off.
Confirm them in the user pool with
`aws cognito-idp admin-confirm-sign-up --user-pool-id "$COGNITO_USER_POOL_ID" --username "<their Cognito username>"`,
or delete them and add them again as in [Adding a user](#adding-a-user).

## Adding a user

(Cognito onboarding by an administrator.) Users cannot register themselves unless
`COGNITO_SELF_SIGNUP_ENABLED=true` (see
[Self sign-up](#self-sign-up)). An administrator
creates each one in the user pool, with a permanent password, and their first sign-in creates
their account on the platform.

1. **Once per pool, create the role groups.** The names are the keys of
   `COGNITO_GROUP_ROLE_MAPPING` (`Admins`, `Developers`, `Analysts`, `Viewers`; see
   [Configuration](#configuration)). The reference pool creates none.

    ```{.bash skip reason="aws: needs the Cognito user pool"}
    aws cognito-idp create-group --user-pool-id "$COGNITO_USER_POOL_ID" --group-name Developers
    ```

2. **Create the user.** Choose a username that is not an email address: the reference pool
   lets people sign in with their email address too, so a username in email form is refused.
   `email_verified=true` lets them reset their own password. `--message-action SUPPRESS`
   stops Cognito emailing a temporary password, which cannot be used to sign in through the
   API (see step 6). The reference pool also requires `given_name` and `family_name`.

    ```{.bash skip reason="aws: needs the Cognito user pool"}
    aws cognito-idp admin-create-user --user-pool-id "$COGNITO_USER_POOL_ID" --username jdoe --user-attributes Name=email,Value=jane.doe@example.com Name=email_verified,Value=true Name=given_name,Value=Jane Name=family_name,Value=Doe --message-action SUPPRESS
    ```

3. **Give them a permanent password.** `--permanent` is required. The password must meet the
   pool's policy: in the reference pool, at least 8 characters with upper case, lower case, a
   digit and a symbol. Type it at the `Password:` prompt, which does not show it.

    ```{.bash skip reason="aws: needs the Cognito user pool"}
    printf 'Password: '; read -rs PASSWORD; printf '\n'
    aws cognito-idp admin-set-user-password --user-pool-id "$COGNITO_USER_POOL_ID" --username jdoe --password "$PASSWORD" --permanent
    ```

    Then either tell the user the password through a channel you trust, or do not share it
    and let them choose their own: `POST /api/v1/auth/forgot-password` with
    `{"username":"jdoe"}` emails them a code, and `POST /api/v1/auth/reset-password` with
    `username`, `confirmation_code` and `new_password` sets the password.

4. **Add them to their role's group.**

    ```{.bash skip reason="aws: needs the Cognito user pool"}
    aws cognito-idp admin-add-user-to-group --user-pool-id "$COGNITO_USER_POOL_ID" --username jdoe --group-name Developers
    ```

5. **First sign-in.** The user calls `POST /api/v1/auth/token` (form fields `username` and
   `password`), then `GET /api/v1/users/me` with the access token. That first authenticated
   call creates their account (see
   [How a sign-in finds its account](#how-a-sign-in-finds-its-account)). The dashboard does
   not yet sign in with Cognito.

6. **If the user cannot sign in with the password you set,** they still have a temporary
   password: step 3 was skipped or run without `--permanent`, or Cognito sent an invitation
   email. A temporary password cannot sign in through the API: `POST /api/v1/auth/token`
   answers `401` with `This user must set a new password before signing in; an
   administrator sets one with admin-set-user-password --permanent.` Run step 3 again with
   `--permanent`.

Do not create the account on the platform first. An account created through the API or the
dashboard has a password and is not linked to the Cognito user, so the user's sign-in would be
refused until an administrator links the two.

## Linking an existing account

An existing account is used for a Cognito sign-in once its `external_id` is set to
`cognito:` followed by the user's Cognito user ID. Do this for an account created on the
platform before Cognito was turned on, for one created by single sign-on, for one an
earlier version of the platform created at a first Cognito sign-in, and after a Cognito
user is deleted and created again (the new user has a new ID).

Print the user's Cognito user ID:

```{.bash skip reason="aws: needs the Cognito user pool"}
aws cognito-idp admin-get-user --user-pool-id "$COGNITO_USER_POOL_ID" --username "<their Cognito username>" --query "UserAttributes[?Name=='sub'].Value | [0]" --output text
```

Then, in the platform's database, set it on the account. `<schema>` is the platform's schema:
`experimentation` unless you changed it (`POSTGRES_SCHEMA`; the Helm chart sets it from
`postgresql.schema`).

```sql
UPDATE <schema>.users
   SET external_id = 'cognito:<the sub printed above>'
 WHERE lower(email) = lower('<their email address on the platform>')
RETURNING id, username, email, external_id;
```

It returns exactly one row. No row means no account has that email address. An error
naming `external_id` means another account is already linked to that Cognito user.

The account keeps its username, email address and password. With `SYNC_ROLES_ON_LOGIN`
on (the default), its role then follows the user's Cognito groups from their next sign-in,
so add the user to the group for the role the account should keep before they sign in.

## If you replace the user pool

A new user pool gives every user a new Cognito user ID, so no account is linked to the
users of the new pool, and their sign-ins are refused. Re-link each account, one at a time:
print the user's ID from the new pool with `admin-get-user` and set it on the account as in
[Linking an existing account](#linking-an-existing-account).

## Reading a refused sign-in

Each refused sign-in writes one WARNING record to the `backend.app.auth.cognito_sign_in`
logger. Its `reason` field is one of these:

| Reason | The sign-in was refused because |
|---|---|
| `not_configured` | `COGNITO_USER_POOL_ID` or `COGNITO_CLIENT_ID` is unset |
| `wrong_issuer` | the token was not an access token issued to the configured user pool and app client |
| `no_sub` | the user's record has no Cognito user ID |
| `local_password` | an account with a password on the platform has the user's username |
| `linked_elsewhere` | an account linked to another identity has the user's username |
| `legacy_unlinked` | an account with no password and no link has the user's username |
| `no_email` | the user has no email address in the pool |
| `email_taken` | another account has the user's email address, in any letter case |
| `field_too_long` | the user's username, email address, given or family name, or Cognito user ID is longer than the platform stores; the record's `field` names which |
| `commit_failed` | the new account could not be saved |

The record also carries `cognito_username`, `sub` and, where an account was involved,
`row_id`, that account's id. For `local_password`, `linked_elsewhere` and
`legacy_unlinked`, `row_id` is the account to link
(see [Linking an existing account](#linking-an-existing-account)). For `field_too_long` it
carries `field`, the column (`username`, `email`, `first_name`, `last_name` or `external_id`), and
never the value: when the over-long value is the username or the Cognito user ID, that one is
left out of the record.

## Configuration

The following settings in `backend/app/core/config.py` control the Cognito integration:

```python
# Cognito settings
COGNITO_GROUP_ROLE_MAPPING: Dict[str, str] = {
    "Admins": "admin",
    "Developers": "developer",
    "Analysts": "analyst",
    "Viewers": "viewer"
}
COGNITO_ADMIN_GROUPS: List[str] = ["Admins", "SuperUsers"]
SYNC_ROLES_ON_LOGIN: bool = True
```

- `COGNITO_GROUP_ROLE_MAPPING`: Maps Cognito group names to application roles
- `COGNITO_ADMIN_GROUPS`: Lists Cognito groups whose members are automatically given superuser status
- `SYNC_ROLES_ON_LOGIN`: When `True` (the default), every authenticated request -- not only
  sign-in -- sets the user's role and superuser status from their Cognito groups. A role
  changed in the dashboard would be overwritten on the user's next request, so
  `PATCH /api/v1/admin/users/{user_id}` refuses a role change with 409 while this is on;
  change the user's group in Cognito instead. Active status is not synced and can be
  changed there.

## User Roles

The application defines the following user roles, in order of decreasing privilege:

1. **ADMIN**: Can create and manage experiments and feature flags, and read all data. Managing users, safety settings, per-flag safety config and rollback, and opening the dashboard's Users, API Keys, Safety settings and Roles pages, also needs a superuser account (see [Role-based access control](rbac/README.md)).
2. **DEVELOPER**: Can create and manage experiments and feature flags
3. **ANALYST**: Can view all data but cannot create or modify experiments/flags
4. **VIEWER**: Reads experiments, feature flags and results, and changes none of them. Like every
   role, a VIEWER can create an API key, and an API key writes tracking data: assignments and
   events.

## Role Assignment Logic

When a user authenticates via Cognito, the following logic determines their role:

1. The user's Cognito groups are read from the `cognito:groups` claim of their access token.
   Cognito leaves the claim out for a user in no group, so a token without it means no groups
2. If the user belongs to any group listed in `COGNITO_ADMIN_GROUPS`, they are:
   - Assigned superuser status (`is_superuser = True`)
   - Given the `ADMIN` role regardless of other group mappings
3. Otherwise, their role is determined by the highest-privilege role mapped from their Cognito groups
4. If the user doesn't belong to any mapped group, they default to the `VIEWER` role

## When a role change takes effect

The groups come from the access token, so adding a user to a group or removing them from one
changes their role on the platform at their next access token, not at their next request.
The reference app client in `infrastructure/cdk/stacks/authentication_stack.py` sets no token
lifetime, so its access tokens last CDK's default of 60 minutes. To make a change take
effect at once, sign the user out of every session, which revokes their tokens:

```{.bash skip reason="aws: needs the Cognito user pool"}
aws cognito-idp admin-user-global-sign-out --user-pool-id "$COGNITO_USER_POOL_ID" --username "<their Cognito username>"
```

With `SYNC_ROLES_ON_LOGIN` on (the default), each request with the new token sets the
account's role and superuser flag from its groups. With it off, the role is set only when the
account is created.

## Example

A user who belongs to both the "Developers" and "Analysts" Cognito groups will be assigned the `DEVELOPER` role in the application.

A user who belongs to the "SuperUsers" group (which is in `COGNITO_ADMIN_GROUPS`) will automatically be assigned the `ADMIN` role and given superuser status, even if "SuperUsers" is mapped to a different role in `COGNITO_GROUP_ROLE_MAPPING`.
