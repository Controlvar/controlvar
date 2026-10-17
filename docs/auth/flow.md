# Authentication Flow Diagrams

This document contains the flow diagrams for the Cognito authentication system implemented in Controlvar.

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

## 1. Registration and Confirmation Flow

Only with `COGNITO_SELF_SIGNUP_ENABLED=true` and a user pool that allows self sign-up; otherwise see [Adding a user](../cognito_integration.md#adding-a-user).

```mermaid
sequenceDiagram
    participant User
    participant Client
    participant API as API Server
    participant Cognito as AWS Cognito
    participant Email as Email Service

    User->>Client: Enter registration information
    Client->>API: POST /auth/signup
    API->>Cognito: SignUp API call
    Cognito->>Email: Send verification code
    Cognito-->>API: Return UserSub (successful registration)
    API-->>Client: Return success + instructions
    Client-->>User: Show verification required message
    
    User->>Client: Enter verification code
    Client->>API: POST /auth/confirm
    API->>Cognito: ConfirmSignUp API call
    Cognito-->>API: Confirm success
    API-->>Client: Account confirmed
    Client-->>User: Show registration complete
```

## 2. Sign-in and Token Issuance Flow

```mermaid
sequenceDiagram
    participant User
    participant Client
    participant API as API Server
    participant Cognito as AWS Cognito
    
    User->>Client: Enter credentials
    Client->>API: POST /auth/token
    API->>Cognito: InitiateAuth API call
    
    alt Successful Authentication
        Cognito-->>API: Return tokens (Access, ID, Refresh)
        API-->>Client: Return tokens + metadata
        Client->>Client: Store tokens securely
        Client-->>User: Redirect to dashboard/home
    else Cognito asks for another step (a new password, MFA)
        Cognito-->>API: Return challenge
        API-->>Client: 401 (this API does not answer challenges)
        Client-->>User: Show error message
    else Failed Authentication
        Cognito-->>API: Authentication error
        API-->>Client: Return error message
        Client-->>User: Show error message
    end
```

## 3. Password Reset Flow

```mermaid
sequenceDiagram
    participant User
    participant Client
    participant API as API Server
    participant Cognito as AWS Cognito
    participant Email as Email Service
    
    User->>Client: Request password reset
    Client->>API: POST /auth/forgot-password
    API->>Cognito: ForgotPassword API call
    Cognito->>Email: Send reset code
    Cognito-->>API: Reset code sent
    API-->>Client: Success message
    Client-->>User: Show code entry UI
    
    User->>Client: Enter code + new password
    Client->>API: POST /auth/reset-password
    API->>Cognito: ConfirmForgotPassword API call
    
    alt Successful Reset
        Cognito-->>API: Password updated
        API-->>Client: Success message
        Client-->>User: Show login prompt
    else Invalid Code
        Cognito-->>API: Code invalid/expired
        API-->>Client: Error message
        Client-->>User: Show error, prompt to try again
    else Password Policy Failure
        Cognito-->>API: Password doesn't meet requirements
        API-->>Client: Password policy error
        Client-->>User: Show password requirements
    end
```

## 4. Token Refresh Mechanism

```mermaid
sequenceDiagram
    participant Client
    participant API as API Server
    participant Cognito as AWS Cognito
    
    Note over Client: Access token expires
    Client->>Client: Detect expired token
    Client->>API: POST /auth/refresh (with refresh token)
    API->>Cognito: InitiateAuth (REFRESH_TOKEN flow)
    
    alt Successful Refresh
        Cognito-->>API: New access & ID tokens
        API-->>Client: Return new tokens
        Client->>Client: Update stored tokens
    else Expired Refresh Token
        Cognito-->>API: Token expired error
        API-->>Client: Authentication required
        Client->>Client: Clear stored tokens
        Client->>Client: Redirect to login
    else Invalid Token
        Cognito-->>API: Invalid token error
        API-->>Client: Authentication required
        Client->>Client: Clear stored tokens
        Client->>Client: Redirect to login
    end
```

## 5. Authorization Decision Flow

```mermaid
flowchart TD
    A[Request to protected endpoint] --> B{Has Authorization header?}
    B -->|Yes| C{Extract token}
    B -->|No| D[Return 401 Unauthorized]
    
    C -->|Success| E{Validate token}
    C -->|Failure| D
    
    E -->|Valid| F{Check token expiration}
    E -->|Invalid| D
    
    F -->|Not expired| G{Check permissions/roles}
    F -->|Expired| D
    
    G -->|Has permission| I[Process request]
    G -->|No permission| J[Return 403 Forbidden]
```
