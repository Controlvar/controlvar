# Testing the Cognito Sign-in Flows

How to exercise the API's Cognito endpoints against your own user pool, and what
each one answers.

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

## Before you start

Start the API with `AUTH_PROVIDER=cognito`, and with `COGNITO_USER_POOL_ID`,
`COGNITO_CLIENT_ID` and `AWS_REGION` set to your user pool's.

## The endpoints

Each is a `POST` under `/api/v1/auth/`, and none needs a token. `/token` takes a
form (`application/x-www-form-urlencoded`); the others take JSON.

| Endpoint | Body | Answers |
|---|---|---|
| `/signup` | `username`, `password`, `email`, `given_name`, `family_name` | `201`, with `user_id`, `confirmed` (`false`) and `message` |
| `/confirm` | `username`, `confirmation_code` | `200`, with `message` and `confirmed` (`true`) |
| `/token` | `username`, `password` | `200`, with `access_token`, `id_token`, `refresh_token`, `expires_in` and `token_type` |
| `/forgot-password` | `username` | `200`, with `message` |
| `/reset-password` | `username`, `confirmation_code`, `new_password` | `200`, with `message` |
| `/refresh` | `refresh_token` | `200`, with `access_token`, `id_token`, `expires_in` and `token_type` |

A request Cognito refuses answers `400`, or `401` at `/token` and `/refresh`. A
body with a field missing answers `422`.

To sign in, with the user's name and password in `COGNITO_USERNAME` and
`COGNITO_PASSWORD`:

```{.bash skip reason="idp: needs a Cognito user pool"}
curl -s -X POST http://localhost:8000/api/v1/auth/token \
  --data-urlencode "username=$COGNITO_USERNAME" \
  --data-urlencode "password=$COGNITO_PASSWORD"
```
