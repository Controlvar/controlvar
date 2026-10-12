# Authentication Documentation

This directory contains documentation related to authentication and authorization in Experimently.

## Files

1. [Authentication Flow](flow.md)
   - Sign-up and login flows
   - Token management
   - Session handling
   - Password reset process
   - Flow diagrams

2. [User Guide](auth-user-guide.md)
   - User registration
   - Account management
   - Password policies
   - MFA setup
   - Role management

3. [Testing Guide](cognito-auth-testing.md)
   - Unit testing
   - Integration testing
   - E2E testing
   - Mock configurations
   - Test data setup

4. [SSO (OIDC, the `sso` module)](sso.md)
   - SAML 2.0 sign-in is not available yet; use OIDC
   - OIDC setup (Google Workspace, GitHub, Microsoft, Auth0)
   - JIT user provisioning
   - Group-to-role mapping
   - Enforced SSO configuration
   - Environment variables reference

## Common Tasks

1. **User Management**
   - [Creating Users](auth-user-guide.md)
   - [Managing Roles](auth-user-guide.md)
   - [Password Reset](auth-user-guide.md#password-reset-process)

2. **Development**
   - [Cognito settings](../cognito_integration.md#required-settings)
   - [Testing](cognito-auth-testing.md)

3. **Security**
   - [Token Management](flow.md)
   - [MFA Setup](auth-user-guide.md)

4. **SSO (the `sso` module)**
   - SAML 2.0 sign-in is not available yet; use OIDC
   - [Google OIDC Setup](sso.md#google-workspace-oidc)
   - [GitHub OIDC Setup](sso.md#github-oidc)
   - [Okta OIDC Setup](sso.md#okta-oidc)
   - [Role Mapping](sso.md#group-to-role-mapping)

## Need Help?

- Check the [User Guide](auth-user-guide.md) for user management
- See the [Flow Documentation](flow.md) for process understanding
- Contact the security team for additional assistance
