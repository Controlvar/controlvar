"""python3-saml's own validation, through ``_parse_saml_response_with_library`` (#1150).

The other SAML tests set ``_SAML_AVAILABLE`` to False, mock the parse, or hand
the library XML it cannot read. These hand it well-formed SAML 2.0 responses
and replace nothing:

* the control: a genuine assertion signed by the configured IdP certificate is
  accepted, with the email and groups it asserts. Without it the refusals
  below would prove nothing; a path that refused everything would pass them;
* refused with 401, each built by the same code as the control: an unsigned
  assertion; one signed by a different key; an extra unsigned assertion next
  to a signed one; a signed assertion edited after signing (its NameID, or one
  of its groups); and, with the configured certificate empty, an unsigned and
  a differently-signed assertion.

``provision_user`` maps the groups this returns through ``role_mapping``, for
a new user and for an existing one, so what this path accepts sets roles.

The keys, certificates and signed responses are made when the tests run, by
``cryptography`` and the library's own signer; none is committed.
"""

from __future__ import annotations

import base64
import datetime
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service

pytestmark = [pytest.mark.unit]

SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAMLP_NS = "urn:oasis:names:tc:SAML:2.0:protocol"

IDP_ENTITY_ID = "https://idp.example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"
GROUPS = ["analysts", "experimenters"]

#: What ``_parse_saml_response_with_library`` raises for every refused case:
#: 401, with the library's error code (its reason is not in the detail).
REFUSED = (401, "SAML validation failed: invalid_response")


@dataclass(frozen=True)
class SigningKey:
    private_key_pem: str
    certificate_pem: str


def _new_signing_key(common_name: str) -> SigningKey:
    """A new RSA key and a self-signed certificate for it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return SigningKey(
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode(),
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode(),
    )


@pytest.fixture(scope="module")
def library() -> None:
    # A failure, not a skip, as in test_saml_acs_answers.py: without
    # python3-saml this module would test nothing.
    assert sso_service._SAML_AVAILABLE is True, "python3-saml is not importable"


@pytest.fixture(scope="module")
def idp_key(library) -> SigningKey:
    """The key of the IdP the configuration names; its certificate is configured."""
    return _new_signing_key("idp.example.com")


@pytest.fixture(scope="module")
def other_key(library) -> SigningKey:
    """A key the configuration does not name."""
    return _new_signing_key("other.example.com")


def _config(certificate: str) -> SSOConfig:
    return SSOConfig(
        id=uuid.uuid4(),
        org_name="Example",
        org_domain="example.com",
        provider_type=SSOProviderType.SAML,
        entity_id=IDP_ENTITY_ID,
        sso_url=f"{IDP_ENTITY_ID}/sso",
        x509_certificate=certificate,
        role_mapping={},
        is_enforced=False,
        is_active=True,
    )


def _instant(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _assertion(email: str, groups: List[str]) -> str:
    """An unsigned assertion, from the configured IdP, for this service provider."""
    now = datetime.datetime.now(datetime.timezone.utc)
    not_on_or_after = _instant(now + datetime.timedelta(minutes=5))
    assertion_id = f"_{uuid.uuid4().hex}"
    values = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
    return (
        f'<saml:Assertion xmlns:saml="{SAML_NS}" ID="{assertion_id}" Version="2.0"'
        f' IssueInstant="{_instant(now)}">'
        f"<saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>"
        "<saml:Subject>"
        '<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">'
        f"{email}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{not_on_or_after}"'
        f' Recipient="{sso_service.settings.SAML_SP_ACS_URL}"/>'
        "</saml:SubjectConfirmation>"
        "</saml:Subject>"
        f'<saml:Conditions NotBefore="{_instant(now - datetime.timedelta(minutes=1))}"'
        f' NotOnOrAfter="{not_on_or_after}">'
        "<saml:AudienceRestriction>"
        f"<saml:Audience>{sso_service.settings.SAML_SP_ENTITY_ID}</saml:Audience>"
        "</saml:AudienceRestriction>"
        "</saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_instant(now)}" SessionIndex="{assertion_id}">'
        "<saml:AuthnContext><saml:AuthnContextClassRef>"
        "urn:oasis:names:tc:SAML:2.0:ac:classes:Password"
        "</saml:AuthnContextClassRef></saml:AuthnContext>"
        "</saml:AuthnStatement>"
        f'<saml:AttributeStatement><saml:Attribute Name="groups">{values}'
        "</saml:Attribute></saml:AttributeStatement>"
        "</saml:Assertion>"
    )


def _response(*assertions: str) -> str:
    """A successful SAML response carrying *assertions*, in order."""
    now = _instant(datetime.datetime.now(datetime.timezone.utc))
    return (
        f'<samlp:Response xmlns:samlp="{SAMLP_NS}" xmlns:saml="{SAML_NS}"'
        f' ID="_{uuid.uuid4().hex}" Version="2.0" IssueInstant="{now}"'
        f' Destination="{sso_service.settings.SAML_SP_ACS_URL}">'
        f"<saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>"
        "<samlp:Status>"
        '<samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
        "</samlp:Status>" + "".join(assertions) + "</samlp:Response>"
    )


def _signed(assertion: str, key: SigningKey) -> str:
    """*assertion* with an enveloped signature by *key*, from the library's signer."""
    # Imported here so that, without python3-saml, the `library` fixture's
    # message is what a run reports rather than a collection error.
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    signed = OneLogin_Saml2_Utils.add_sign(
        assertion, key.private_key_pem, key.certificate_pem
    )
    return signed.decode() if isinstance(signed, bytes) else signed


def _edited(xml: str, old: str, new: str) -> str:
    assert xml.count(old) == 1, f"{old!r} is not in the response exactly once"
    return xml.replace(old, new)


def _parse(certificate: str, response_xml: str) -> Dict:
    encoded = base64.b64encode(response_xml.encode()).decode()
    return sso_service._parse_saml_response_with_library(_config(certificate), encoded)


def test_a_genuine_assertion_signed_by_the_configured_certificate_is_accepted(
    idp_key: SigningKey,
):
    response = _response(_signed(_assertion(ALICE, GROUPS), idp_key))

    user_info = _parse(idp_key.certificate_pem, response)

    assert user_info["email"] == ALICE
    assert user_info["name_id"] == ALICE
    assert user_info["groups"] == GROUPS


#: Each case: (the configured certificate, the response), from the IdP's key
#: and the other one.
Case = Callable[[SigningKey, SigningKey], Tuple[str, str]]

REFUSED_CASES: Dict[str, Case] = {
    "unsigned": lambda idp, other: (
        idp.certificate_pem,
        _response(_assertion(BOB, GROUPS)),
    ),
    "signed-by-a-different-key": lambda idp, other: (
        idp.certificate_pem,
        _response(_signed(_assertion(BOB, GROUPS), other)),
    ),
    "extra-unsigned-assertion-next-to-a-signed-one": lambda idp, other: (
        idp.certificate_pem,
        _response(
            _assertion(BOB, ["admins"]),
            _signed(_assertion(ALICE, GROUPS), idp),
        ),
    ),
    "edited-after-signing-nameid": lambda idp, other: (
        idp.certificate_pem,
        _edited(
            _response(_signed(_assertion(ALICE, GROUPS), idp)),
            f">{ALICE}<",
            f">{BOB}<",
        ),
    ),
    "edited-after-signing-group": lambda idp, other: (
        idp.certificate_pem,
        _edited(
            _response(_signed(_assertion(ALICE, GROUPS), idp)),
            f">{GROUPS[0]}<",
            ">admins<",
        ),
    ),
    "certificate-empty-unsigned": lambda idp, other: (
        "",
        _response(_assertion(BOB, GROUPS)),
    ),
    "certificate-empty-signed-by-a-different-key": lambda idp, other: (
        "",
        _response(_signed(_assertion(BOB, GROUPS), other)),
    ),
}


@pytest.mark.parametrize("case", list(REFUSED_CASES))
def test_the_library_refuses(case: str, idp_key: SigningKey, other_key: SigningKey):
    certificate, response = REFUSED_CASES[case](idp_key, other_key)

    try:
        accepted = _parse(certificate, response)
    except HTTPException as exc:
        assert (exc.status_code, exc.detail) == REFUSED
    else:
        pytest.fail(
            f"{case} was accepted: email={accepted['email']!r} "
            f"groups={accepted['groups']!r}"
        )
