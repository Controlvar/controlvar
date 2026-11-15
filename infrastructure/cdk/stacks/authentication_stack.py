from dataclasses import dataclass
from typing import Optional

from aws_cdk import (
    Stack,
    aws_cognito as cognito,
    Duration,
    CfnOutput,
    Fn,
)
from constructs import Construct

from stacks.environments import data_removal_policy

#: The CDK context key that turns on dashboard sign-in through the Cognito
#: Hosted UI (#708): `-c cognito_hosted_ui=true`. Unset or false, this stack
#: synthesises exactly what it did before the key existed, and the API task is
#: told nothing about Cognito (infrastructure/tests/test_cognito_hosted_ui.py).
HOSTED_UI_CONTEXT = "cognito_hosted_ui"

#: The Hosted UI domain's prefix, `controlvar-<env>` unless this context key
#: names another. A prefix is global to its region, across every AWS account,
#: so a taken one fails the deploy; it always ends in `-<env>`, so no two
#: environments ask for the same one.
DOMAIN_PREFIX_CONTEXT = "cognito_domain_prefix"
DEFAULT_DOMAIN_PREFIX = "controlvar"

#: botocore's model for the domain: `^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?$`.
_PREFIX_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
#: Words the Cognito developer guide says a prefix may not contain. The API
#: model does not say so; refusing them here costs a name nobody needs.
_RESERVED_IN_PREFIX = ("aws", "amazon", "cognito")


@dataclass(frozen=True)
class HostedUi:
    """What the API task is given when the Hosted UI is on.

    The ids are CloudFormation references to this stack, so a stack that uses
    them imports this stack's exports, which pins the pool, the client and the
    domain until that stack stops using them.
    """

    user_pool_id: str
    client_id: str
    #: `<prefix>.auth.<region>.amazoncognito.com`, the value COGNITO_DOMAIN
    #: takes: a domain name, no scheme (the domain's own `Ref` is the bare
    #: prefix, and its `base_url()` starts with `https://`).
    domain: str
    region: str
    callback_url: str
    logout_url: str


def hosted_ui_enabled(scope: Construct) -> bool:
    """Whether `-c cognito_hosted_ui=true` was given.

    `true` or `false`, as the CLI passes them (strings) or as cdk.json can
    (booleans); unset is false. Anything else is refused rather than read as
    one or the other.
    """
    value = scope.node.try_get_context(HOSTED_UI_CONTEXT)
    if value is None or value is False or value == "false":
        return False
    if value is True or value == "true":
        return True
    raise ValueError(
        f"the {HOSTED_UI_CONTEXT} context must be true or false; got {value!r}"
    )


def hosted_ui_domain_prefix(scope: Construct, environment: str) -> str:
    """The Hosted UI domain's prefix for ``environment``.

    Refused unless it is at most 63 lower-case letters, digits and hyphens,
    not starting with a hyphen, free of the reserved words, and ending in
    `-<environment>`, so it ends in a letter. The suffix alone is refused.
    """
    prefix = scope.node.try_get_context(DOMAIN_PREFIX_CONTEXT)
    if prefix is None:
        return f"{DEFAULT_DOMAIN_PREFIX}-{environment}"
    suffix = f"-{environment}"
    if not (
        isinstance(prefix, str)
        and len(suffix) < len(prefix) <= 63
        and set(prefix) <= _PREFIX_CHARACTERS
        and prefix.endswith(suffix)
        and not prefix.startswith("-")
        and not any(word in prefix for word in _RESERVED_IN_PREFIX)
    ):
        raise ValueError(
            f"the {DOMAIN_PREFIX_CONTEXT} context must be at most 63 lower-case "
            "letters, digits and hyphens, not starting with a hyphen, without "
            f"{', '.join(_RESERVED_IN_PREFIX)}, and ending in {suffix!r}; "
            f"got {prefix!r}"
        )
    return prefix


class AuthenticationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        environment: str = "dev",
        public_base_url: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        hosted_ui = hosted_ui_enabled(self)
        if not hosted_ui and self.node.try_get_context(DOMAIN_PREFIX_CONTEXT) is not None:
            raise ValueError(
                f"the {DOMAIN_PREFIX_CONTEXT} context does nothing unless "
                f"{HOSTED_UI_CONTEXT} is true"
            )
        origin = None
        if hosted_ui:
            # The dashboard and the API share one origin (DECISIONS D14), so
            # the dashboard's callback and sign-out pages are on PUBLIC_BASE_URL.
            # Cognito accepts an http:// callback for localhost only.
            origin = (public_base_url or "").strip().rstrip("/")
            if not origin.startswith("https://"):
                raise ValueError(
                    f"{HOSTED_UI_CONTEXT} needs PUBLIC_BASE_URL, the https:// origin "
                    "users reach the dashboard at: its callback and sign-out pages "
                    f"are registered on it; got {public_base_url!r}"
                )

        # Define the Cognito User Pool
        self.user_pool = cognito.UserPool(
            self,
            "ExperimentationUserPool",
            user_pool_name=f"experimentation-platform-users-{environment}",
            # Administrators create users; nobody can register themselves.
            # The dashboard says the same ("Accounts are created by an
            # administrator"). Pinned by tests/test_auth_stack.py.
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(
                email=True,
                username=True,
            ),
            # Password policy
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(7),
            ),
            # Advanced security features
            advanced_security_mode=cognito.AdvancedSecurityMode.OFF,
            # Account recovery settings
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # Email settings
            email=cognito.UserPoolEmail.with_cognito(
                reply_to="support@controlvar.com",
            ),
            # User attributes
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(
                    required=True,
                    mutable=True,
                ),
                given_name=cognito.StandardAttribute(
                    required=True,
                    mutable=True,
                ),
                family_name=cognito.StandardAttribute(
                    required=True,
                    mutable=True,
                ),
                phone_number=cognito.StandardAttribute(
                    required=False,
                    mutable=True,
                ),
            ),
            # Auto-verification settings are not directly configurable in aws_cognito.
            # MFA configuration
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(
                sms=True,
                otp=True,
            ),
            # Kept on teardown in prod only (stacks/environments.py). It was
            # RETAIN everywhere but dev, so a staging teardown left a named
            # pool behind and the next staging deploy collided with it.
            removal_policy=data_removal_policy(environment),
        )

        scopes = [
            cognito.OAuthScope.OPENID,
            cognito.OAuthScope.EMAIL,
            cognito.OAuthScope.PROFILE,
        ]
        callback_urls = ["https://example.com/callback"]
        logout_urls = ["https://example.com/logout"]
        hosted_ui_client = {}
        if hosted_ui:
            # GetUser, which checks every request, requires this scope.
            scopes.append(cognito.OAuthScope.COGNITO_ADMIN)
            callback_urls = [f"{origin}/auth/callback"]
            logout_urls = [f"{origin}/login"]
            # No explicit generate_secret. GenerateSecret is one of the two
            # client properties whose change replaces the client (the CDK
            # CLI's resource spec; UserPoolId is the other), and this template
            # has never had it: even `false` risks a new client id, which every
            # deployment is configured with. The four changes here do not.
            hosted_ui_client = {"prevent_user_existence_errors": True}

        # Add custom attributes
        # Add custom attributes to the User Pool Client
        self.user_pool_client = self.user_pool.add_client(
            "ExperimentationUserPoolClient",
            user_pool_client_name=f"experimentation-platform-client-{environment}",
            auth_flows=cognito.AuthFlow(
                user_password=True,
                admin_user_password=True,
                custom=True,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                ),
                scopes=scopes,
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            **hosted_ui_client,
        )

        # Output the User Pool ID and ARN for reference
        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="Cognito User Pool ID",
            export_name=f"{construct_id}-UserPoolId",
        )

        CfnOutput(
            self,
            "UserPoolArn",
            value=self.user_pool.user_pool_arn,
            description="Cognito User Pool ARN",
            export_name=f"{construct_id}-UserPoolArn",
        )

        #: None while the Hosted UI is off: the API is then told nothing.
        self.hosted_ui: Optional[HostedUi] = None
        if not hosted_ui:
            return

        # Managed login version 1, the classic Hosted UI: with version 2 a
        # domain shows no sign-in page until a ManagedLoginBranding exists.
        # No custom domain (and so no certificate): the prefix domain only.
        domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=hosted_ui_domain_prefix(self, environment),
            ),
            managed_login_version=cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI,
        )
        self.hosted_ui = HostedUi(
            user_pool_id=self.user_pool.user_pool_id,
            client_id=self.user_pool_client.user_pool_client_id,
            domain=Fn.join(
                "", [domain.domain_name, ".auth.", self.region, ".amazoncognito.com"]
            ),
            region=self.region,
            callback_url=callback_urls[0],
            logout_url=logout_urls[0],
        )
        # What an operator checks after the deploy. No export: nothing
        # imports them, and an export would pin them.
        CfnOutput(
            self,
            "HostedUiDomain",
            value=self.hosted_ui.domain,
            description="The Cognito Hosted UI's domain name (COGNITO_DOMAIN)",
        )
        CfnOutput(
            self,
            "HostedUiCallbackUrl",
            value=self.hosted_ui.callback_url,
            description="The dashboard's sign-in callback registered on the app client",
        )
        CfnOutput(
            self,
            "HostedUiLogoutUrl",
            value=self.hosted_ui.logout_url,
            description="Where the Hosted UI returns a signed-out user",
        )
