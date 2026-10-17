# Authentication User Guide

This guide explains how to use the authentication features of Controlvar, including registration, login, password management, and security best practices.

!!! note "Cognito only"
    The password-reset and token-refresh endpoints (`POST /api/v1/auth/forgot-password`,
    `/reset-password` and `/refresh`) are available only when `AUTH_PROVIDER=cognito`.
    The sign-up and confirmation endpoints (`POST /api/v1/auth/signup` and `/confirm`)
    also need `COGNITO_SELF_SIGNUP_ENABLED=true`; without it they answer 404 and an
    administrator creates users ([Adding a user](../cognito_integration.md#adding-a-user)).
    With any other provider, including the default `local`, all five answer 404. With
    `local`, sign in with `POST /api/v1/auth/login`; an administrator creates accounts
    and resets passwords.

    The dashboard does not yet sign in with Cognito. Sign in through the API with
    `POST /api/v1/auth/token`. SSO sign-in needs `AUTH_PROVIDER=local`.

## Account Registration

Only with `COGNITO_SELF_SIGNUP_ENABLED=true` and a user pool that allows self sign-up; otherwise see [Adding a user](../cognito_integration.md#adding-a-user).

### Registration Requirements

To create an account on Controlvar, you will need to provide:

- **Username**: 3 to 50 characters
- **Email**: A valid email address you can access for verification
- **Password**: Must satisfy the following requirements:
  - At least 8 characters
  - At most 100 characters
  - At least one uppercase letter
  - At least one lowercase letter
  - At least one digit (0-9)
- **First Name** and **Last Name**: Your real name or preferred name

### Registration Process

The dashboard has no registration page and no sign-up link: registration is through the API
only, and only with `COGNITO_SELF_SIGNUP_ENABLED=true`. Without it, both endpoints below
answer 404 and an administrator creates your account
([Adding a user](../cognito_integration.md#adding-a-user)).

1. `POST /api/v1/auth/signup` with `username`, `password`, `email`, `given_name` and
   `family_name`. Cognito emails a verification code to the address.
2. `POST /api/v1/auth/confirm` with `username` and the `confirmation_code` from the email.
3. Sign in with `POST /api/v1/auth/token` (form fields `username` and `password`). The
   dashboard does not yet sign in with Cognito.

### Email Verification

- If you don't receive a verification code, check your spam folder

## Login Process

### Standard Login

1. Navigate to the login page at `/login`
2. Enter your email address and password
3. Click "Sign in"

This is the sign-in for `AUTH_PROVIDER=local`. With `cognito`, the dashboard does not yet
sign in; see the note at the top of this page.

### Login Security Features

- **Session Management**: A sign-in lasts 12 hours by default (`LOCAL_AUTH_TOKEN_TTL_MINUTES`); then you sign in again
- **Account Protection**: Too many failed login attempts will temporarily lock your account

### After Login

Once logged in successfully:
- You will be redirected to the dashboard
- An access token is stored in your browser
- This token is used for all subsequent API requests

## Password Management

### Password Reset Process

The dashboard has no reset page and no reset link, under any `AUTH_PROVIDER`. The login
page's **Forgot password?** only says "Ask an administrator to reset it." What happens
next depends on the provider.

**With `AUTH_PROVIDER=local` (the default)** there is no self-service reset. An
administrator with a superuser account sets a new password for you through the API, without
knowing the old one: `PUT /api/v1/admin/users/{user_id}` with a JSON body of
`{"password": "<new password>"}` and the administrator's bearer token.
`PUT /api/v1/users/{user_id}` accepts the same `password` field. The dashboard's **Edit**
dialog on **Admin → Users** changes the role and whether the account is active, not the
password. Nobody can reset their own password this way, administrators included; sign in
with the new password and then change it yourself (see
[Changing Your Password](#changing-your-password)).

**With `AUTH_PROVIDER=cognito`** the password lives in the Cognito user pool, and either of
these works:

- an administrator sets it with
  `aws cognito-idp admin-set-user-password ... --permanent`, as in step 3 of
  [Adding a user](../cognito_integration.md#adding-a-user);
- you reset it yourself through the API, if your account's email address is verified
  (`email_verified=true`): `POST /api/v1/auth/forgot-password` with
  `{"username": "..."}` emails you a code, and `POST /api/v1/auth/reset-password` with
  `username`, `confirmation_code` and `new_password` sets the new password. Then sign in
  with `POST /api/v1/auth/token`.

### Password Reset Notes

- New passwords must meet the same requirements as registration
- Under `cognito`, a password set in the dashboard's database (the `local` route above)
  does not change the Cognito password, so it does not change how you sign in

### Changing Your Password

With the default `local` provider, open **More** in the dashboard's top navigation and choose
**Change password** (in a window narrower than 1280 px, open the menu button first). The page
(`/account/password`) asks for your current password and the new one twice. The link is not
shown under `cognito`, where the password lives in the user pool. An account created through
single sign-on has no password of its own, and the page says so.

Through the API, send your current password and the new one to
`POST /api/v1/users/me/password`; see
[Change Your Own Password](../api/auth.md#change-your-own-password) for a runnable example.

- A wrong current password is refused with `403` and counts toward the same limit as
  failed logins.
- The new password must meet the same requirements as registration.
- Changing your password does **not** sign out your other sessions: a token issued before
  the change keeps working until it expires. To end them straight away, an administrator
  deactivates the account (sets `is_active` to `false`).
- An administrator resets **another** user's password with `PUT /api/v1/admin/users/{id}`;
  nobody, administrators included, can change their own password that way.

## Using Authentication Tokens

The platform uses JWT (JSON Web Tokens) for authentication. Here's how they work:

### Token Types

- **With `AUTH_PROVIDER=local`** (the default), `POST /api/v1/auth/login` returns one access token, valid for 12 hours by default; there is no ID token and no refresh token.
- **With `AUTH_PROVIDER=cognito`**, `POST /api/v1/auth/token` returns Cognito's access token, ID token and refresh token.

### Sample Token Usage

When making API requests, include the access token in the Authorization header:

```javascript
// Example of an authenticated API request using fetch
async function fetchExperiments() {
  const response = await fetch('https://api.controlvar.example.com/api/v1/experiments', {
    method: 'GET',
    headers: {
      'Authorization': `Bearer ${localStorage.getItem('accessToken')}`,
      'Content-Type': 'application/json'
    }
  });
  
  return response.json();
}
```

### Token Refresh

Under `cognito`, when your access token expires, use the refresh token to get a new one:

```javascript
// Example of refreshing an expired token
async function refreshTokens() {
  const refreshToken = localStorage.getItem('refreshToken');
  
  const response = await fetch('https://api.controlvar.example.com/api/v1/auth/refresh', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ refresh_token: refreshToken })
  });
  
  const data = await response.json();
  
  if (response.ok) {
    localStorage.setItem('accessToken', data.access_token);
    localStorage.setItem('idToken', data.id_token);
    return true;
  } else {
    // Refresh failed, user needs to log in again
    return false;
  }
}
```

## Managing users in the dashboard

The dashboard's **Admin → Users** page (`/admin/users`) lists every account and
lets you invite, edit and delete them. Opening it needs a **superuser** account,
whatever its role; the table's **Superuser** column shows who has one. The Admin role
alone does not open the admin area, and the dashboard cannot make an existing account a
superuser.

- **Invite User** creates an account with a temporary password, shown once. An
  account invited with the Admin role is also made a superuser.
- **Edit** changes an account's role, whether it is active, or both. Save is
  available once something has changed, and only what changed is sent
  (`PATCH /api/v1/admin/users/{user_id}`, see the
  [API reference](../api/endpoints.md)). After a save the list reloads on the
  page and search you were on.
- **Delete** removes the account after you confirm it.

The four roles are Admin, Developer, Analyst and Viewer. A legacy account with no
role shows "No role" in the table; pick one in the Edit dialog. Custom roles in
the full edition are assigned through the RBAC API (`POST /api/v1/rbac/roles/assign`), not
here.

Rules the page and the API both apply:

- You can't change your own role or deactivate yourself; ask another
  administrator. You can't delete your own account either.
- Through the API (`PUT /api/v1/admin/users/{user_id}` or
  `PUT /api/v1/users/{user_id}`) you also can't remove your own superuser
  access, so keep at least two superusers; with Cognito role sync on, though,
  a superuser removed from the admin group in Cognito loses superuser access
  on their next request, and nothing here stops that.
- Deactivating a user stops them signing in, refuses requests with a token they
  already hold, and **stops the API keys they created, including keys your
  applications use**. Check which keys a user created before deactivating them.
- With `AUTH_PROVIDER=cognito` and `SYNC_ROLES_ON_LOGIN` on (the default), roles
  come from Cognito groups and are rewritten on every request, so a role change
  is refused with "Roles on this deployment come from Cognito groups and are
  updated on every request. Change this user's group in Cognito instead."
  Deactivating still works.
- With SSO (full edition), a role set here lasts until the user's next SSO
  sign-in with a group in the configuration's `role_mapping`, which replaces it
  (see [Group-to-Role Mapping](sso.md#group-to-role-mapping)).

When a save fails, the dialog stays open with your choices and says why. "You
don't have permission to change users" means your account is no longer a
superuser; sign in again to check. "This user no longer exists" means someone
deleted the account; closing the dialog refreshes the list.

## Common Error Scenarios and Solutions

### Registration Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `422`, at `["body", "email"]` | Email address is not valid | Check for typos in your email |
| `422`, at `["body", "password"]`, naming the rule it breaks | The password breaks a requirement above | Follow the password requirements listed above |

### Login Errors

What the dashboard's sign-in form shows (`AUTH_PROVIDER=local`):

| Error | Cause | Solution |
|-------|-------|----------|
| "Email or password is incorrect." | No account has this email, the password does not match, or the account is deactivated. The API answers `401` the same way for all three | Check both. An administrator can reset the password or reactivate the account |
| "Too many failed login attempts; account temporarily locked. Retry in N seconds." | Ten failed sign-ins for this email within 15 minutes (`LOCAL_AUTH_MAX_FAILED_ATTEMPTS`, `LOCAL_AUTH_LOCKOUT_MINUTES`); the API answers `423` | Wait the seconds it names. Every attempt until then is refused, even with the right password |
| "Too many attempts. Please wait a moment and try again." | More than ten sign-ins in a minute from one address (`429`) | Wait a minute |
| "Can't reach the API at …" | The dashboard got no answer from the API | Check that the API is running and that the dashboard's origin is in `CORS_ORIGINS` |

### Token Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `401` "Could not validate credentials" | The access token has expired, or is malformed or altered | Log in again (under `cognito`, you can also refresh the token) |

### Password Reset Errors

These come from the Cognito API routes only. The route answers `400` with Cognito's own
message, which names the error:

| Error in the message | Cause | Solution |
|-------|-------|----------|
| `CodeMismatchException` | The code does not match the one emailed | Check the code, or request a new one |
| `ExpiredCodeException` | The code has expired | Request a new code with `/auth/forgot-password` |
| `InvalidPasswordException` | The new password does not meet the pool's policy | Follow the pool's password requirements |
| `LimitExceededException` | Too many attempts | Wait before trying again |

## Security Best Practices

To keep your account secure:

### Password Security

- Use a unique password not used on other websites
- Consider using a password manager
- Change your password if you think someone else may know it
- Never share your password with anyone

### Account Protection

- Don't share your verification codes with anyone
- Log out when using shared or public computers
- Check for HTTPS in the browser address bar before logging in

### Device Security

- Keep your devices and browsers updated
- Use antivirus software on your computers
- Be cautious when logging in from public networks
- Clear browser cookies and storage after using public computers

### Suspicious Activity

If you notice suspicious activity on your account:

1. Ask an administrator to deactivate the account (`is_active` set to `false`). That ends
   every session straight away; changing the password alone does not.
2. Have the administrator reset your password, and reactivate the account once the tokens
   issued before it was deactivated have expired (12 hours with the default
   `LOCAL_AUTH_TOKEN_TTL_MINUTES`). A deactivated account's tokens are refused only while
   it stays deactivated.
3. The audit log records sign-ins with a password, through Cognito (`POST /auth/token`) or
   through SSO, and an administrator's
   changes to your account's role, superuser flag or active status. A password change and other changes
   to your profile are not recorded (#502); keep the account deactivated while you check.
