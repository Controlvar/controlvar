# Two-step sign-in

With local sign-in (`AUTH_PROVIDER=local`), each account can turn on two-step sign-in (2FA,
using TOTP codes) for itself. Signing in then asks for the password and then for the 6-digit
code an authenticator app on your phone shows, or for one of your recovery codes. Under Cognito,
the dashboard does not offer it: Cognito's own sign-in applies.

The API side, with every route and answer, is in [Two-step sign-in](../api/auth.md#two-step-sign-in).

## Turn it on

1. In the dashboard, open **More → Two-step sign-in** and choose **Set up two-step sign-in**.
2. Scan the QR code with your authenticator app, or type the key shown under it into the app.
   On the phone itself, **Open in an authenticator app on this device** adds it.
3. Enter the code the app shows and choose **Turn on two-step sign-in**. Nothing changes at
   sign-in until a code is accepted.
4. Save the 10 recovery codes: **Download as a text file** or **Copy codes**, tick **I have saved
   these codes**, then choose **Done**. They are shown this once.

A code is accepted in its own 30 seconds and in the 30 seconds either side. If the first code is
refused, check that your phone sets its clock automatically.

## Sign in with a code

After your email and password, enter the code your app shows and choose **Verify**. Spaces and
hyphens in a code are ignored, and each code works once.

Without your phone, choose **Use a recovery code instead** and enter one of your saved codes.
Each recovery code works once; **More → Two-step sign-in** says how many are left.

The code step lasts 5 minutes. After that, sign-in starts again at the password.

## Lost your phone and your recovery codes

Ask an administrator to reset your two-step sign-in. A superuser opens **Admin → Users**, edits
your account and chooses **Reset two-step sign-in**. You then sign in with your password alone and
can set two-step sign-in up again. No one can reset their own. When no superuser can sign in, an
operator resets accounts from the command line:
[Resetting two-step sign-in](auth-user-guide.md#resetting-two-step-sign-in).

## New recovery codes, or turning it off

On **More → Two-step sign-in**, **Make new recovery codes** takes a code from your app and gives a
new set of 10; the codes you had stop working. **Turn off two-step sign-in** takes a code or a
recovery code; signing in then asks for your password alone.

## When a code is refused

Wrong codes count with wrong passwords: after 10 within 15 minutes, sign-in to your email address
is locked until the 15 minutes are up. Two answers last until an administrator resets your
two-step sign-in:

| What you see | Why |
|---|---|
| Two-step sign-in is locked after too many wrong codes. Ask an administrator to reset it. | 100 wrong codes in a row: even the right code is refused |
| Two-step sign-in is unavailable for this account. Ask an administrator to reset it. | The server's `SECRET_KEY` changed, so your stored secret cannot be read |

## Known limits

- A session you already hold stays signed in until it expires, after two-step sign-in is turned
  on or off and after a reset.
- Nothing can require two-step sign-in for a role yet.
- Signing in with SSO (OIDC, with the `sso` module) asks for no code: only the identity provider's
  own checks apply.
- An API rolled back to a release from before the code step asks for no code either:
  [Rolling back past two-step sign-in](../deployment/rollback-runbook.md#rolling-back-past-two-step-sign-in).
- The count of wrong passwords and codes is kept by each API process; the count of 100 wrong
  codes is stored with the account.
