"""SMTP certificate and session behavior, using disposable loopback relays."""

from __future__ import annotations

import ast
import base64
import ipaddress
import logging
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.app.core.config import Settings
from backend.app.services import email_notifier as email
from backend.tests.no_outbound_network import install_guard

USER = "smtp-test-user"
PASSWORD = "smtp-test-password-73"
LOGGER = "backend.app.services.email_notifier"


@pytest.fixture(autouse=True)
def isolated_network_and_trust(monkeypatch, tmp_path):
    attempts = []
    install_guard(monkeypatch, attempts)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent-default.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "absent-default-dir"))
    yield
    assert attempts == []


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("smtp-certificates")
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SMTP test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    ca_file = directory / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    result = {"ca": ca_file}
    for label, san, start, end in [
        ("valid", x509.IPAddress(ipaddress.ip_address("127.0.0.1")), -1, 2),
        ("dns", x509.DNSName("localhost"), -1, 2),
        ("wrong", x509.DNSName("mail.example.org"), -1, 2),
        ("expired", x509.IPAddress(ipaddress.ip_address("127.0.0.1")), -3, -1),
        ("future", x509.IPAddress(ipaddress.ip_address("127.0.0.1")), 1, 3),
    ]:
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        leaf = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(name)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=start))
            .not_valid_after(now + timedelta(days=end))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_file, key_file = directory / f"{label}.pem", directory / f"{label}.key"
        cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        result[label] = (cert_file, key_file)
    # A separately trusted certificate also exercises a self-signed endpoint.
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(leaf_key, hashes.SHA256())
    )
    cert_file, key_file = directory / "self.pem", directory / "self.key"
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    result["self"] = (cert_file, key_file)
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(name)
        .last_update(now)
        .next_update(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    result["crl"] = directory / "crl.pem"
    result["crl"].write_bytes(crl.public_bytes(serialization.Encoding.PEM))
    return result


class Relay:
    """One bounded SMTP session; record verbs and delivered authentication only."""

    def __init__(
        self,
        cert,
        *,
        offer_tls=True,
        reject_tls=False,
        quit_code=221,
        login_error=False,
        data_error=False,
        old_tls=False,
        break_tls=False,
    ):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(*map(str, cert))
        if old_tls:
            self.context.maximum_version = ssl.TLSVersion.TLSv1_1
        self.offer_tls, self.reject_tls = offer_tls, reject_tls
        self.quit_code, self.login_error, self.data_error = (
            quit_code,
            login_error,
            data_error,
        )
        self.break_tls = break_tls
        self.verbs, self.logins, self.errors = [], [], []
        self.accepted, self.data_accepted = False, False
        self.stop = threading.Event()
        self.done = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        conn = stream = None
        try:
            while not self.stop.is_set():
                try:
                    conn, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if conn is None:
                return
            self.accepted = True
            conn.settimeout(3)
            stream = conn.makefile("rb")
            conn.sendall(b"220 loopback SMTP\r\n")
            tls = False
            while True:
                line = stream.readline(16384)
                if not line:
                    break
                verb = line.split(b" ", 1)[0].strip().decode("ascii").upper()
                self.verbs.append(verb)
                if verb == "EHLO":
                    response = b"250-loopback\r\n"
                    if self.offer_tls and not tls:
                        response += b"250-STARTTLS\r\n"
                    conn.sendall(response + b"250 AUTH PLAIN\r\n")
                elif verb == "STARTTLS":
                    if self.reject_tls:
                        conn.sendall(b"454 TLS unavailable\r\n")
                        continue
                    conn.sendall(b"220 Begin TLS\r\n")
                    if self.break_tls:
                        conn.sendall(b"SMTP test protocol failure\r\n")
                        break
                    stream.close()
                    stream = None
                    try:
                        conn = self.context.wrap_socket(conn, server_side=True)
                    except ssl.SSLError:
                        break
                    stream = conn.makefile("rb")
                    tls = True
                elif verb == "AUTH":
                    _, mechanism, payload = line.strip().split(b" ", 2)
                    assert mechanism == b"PLAIN"
                    _, user, password = (
                        base64.b64decode(payload).decode("ascii").split("\0")
                    )
                    self.logins.append((user, password, tls))
                    if self.login_error:
                        conn.sendall(
                            b"535-" + PASSWORD.encode() + b"\r\n535 Login refused\r\n"
                        )
                    else:
                        conn.sendall(b"235 Signed in\r\n")
                elif verb in ("MAIL", "RCPT", "RSET"):
                    conn.sendall(b"250 OK\r\n")
                elif verb == "DATA":
                    conn.sendall(b"354 End with dot\r\n")
                    while True:
                        part = stream.readline(16384)
                        if not part:
                            raise AssertionError("DATA ended without terminator")
                        if part == b".\r\n":
                            break
                    if self.data_error:
                        conn.sendall(b"554 " + PASSWORD.encode() + b"\r\n")
                    else:
                        self.data_accepted = True
                        conn.sendall(b"250 Accepted\r\n")
                elif verb == "QUIT":
                    conn.sendall(f"{self.quit_code} Closing\r\n".encode())
                    break
                else:
                    raise AssertionError(f"Unexpected SMTP verb: {verb}")
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            if stream is not None:
                stream.close()
            if conn is not None:
                conn.close()
            self.listener.close()
            self.done.set()

    def close(self):
        self.stop.set()
        self.thread.join(4)
        assert not self.thread.is_alive(), "SMTP relay did not stop"
        assert not self.errors, self.errors


@pytest.fixture
def relay(certificates):
    relays = []

    def make(label="valid", **kwargs):
        server = Relay(certificates[label], **kwargs)
        relays.append(server)
        return server

    yield make
    for server in relays:
        server.close()


def notifier(
    monkeypatch, server, ca_file="", user=USER, password=PASSWORD, host="127.0.0.1"
):
    monkeypatch.setattr(
        email,
        "settings",
        SimpleNamespace(
            EMAIL_ENABLED=True,
            SENDGRID_API_KEY="",
            SMTP_HOST=host,
            SMTP_PORT=server.port,
            SMTP_USERNAME=user,
            SMTP_PASSWORD=password,
            SMTP_CA_FILE=str(ca_file),
        ),
    )
    return email.EmailNotifier()


def send(instance):
    return instance._send_via_smtp(
        ["recipient@example.com"], "SMTP test", "<p>Test</p>"
    )


def assert_safe(caplog, capfd, *, expected_class=None, password=PASSWORD):
    records = [
        r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR
    ]
    if expected_class:
        assert len(records) == 1
        if expected_class == "SSLError":
            assert issubclass(getattr(ssl, records[0].args[3]), ssl.SSLError)
        else:
            assert expected_class in records[0].getMessage()
    assert all(
        r.exc_info is None and r.stack_info is None
        for r in caplog.records
        if r.name == LOGGER
    )
    output = capfd.readouterr()
    combined = caplog.text + output.out + output.err
    assert password not in combined
    for raw in (f"\0{USER}\0{password}", f"{USER}\0{USER}\0{password}"):
        assert base64.b64encode(raw.encode()).decode() not in combined
    return records


class TestSMTPCertificateChecks:
    @pytest.mark.regression
    def test_a_relay_with_a_self_signed_certificate_gets_no_credentials(
        self, relay, monkeypatch, caplog, capfd
    ):
        server = relay("self")
        assert send(notifier(monkeypatch, server)) is False
        assert server.done.wait(4)
        assert server.verbs == ["EHLO", "STARTTLS"]
        assert server.logins == []
        assert_safe(caplog, capfd, expected_class="SSLCertVerificationError")

    @pytest.mark.parametrize(
        "label,host",
        [("valid", "127.0.0.1"), ("dns", "localhost"), ("self", "127.0.0.1")],
    )
    def test_a_relay_the_ca_file_trusts_gets_the_login_and_the_mail(
        self, relay, certificates, monkeypatch, caplog, capfd, label, host
    ):
        server = relay(label)
        ca = certificates["self"][0] if label == "self" else certificates["ca"]
        monkeypatch.setenv("SMTP_CA_FILE", str(ca))
        setting = Settings(_env_file=None)
        assert (
            send(notifier(monkeypatch, server, setting.SMTP_CA_FILE, host=host)) is True
        )
        assert server.done.wait(4)
        assert server.verbs == [
            "EHLO",
            "STARTTLS",
            "EHLO",
            "AUTH",
            "MAIL",
            "RCPT",
            "DATA",
            "QUIT",
        ]
        assert server.logins == [(USER, PASSWORD, True)]
        assert server.data_accepted
        assert_safe(caplog, capfd)

    def test_without_a_ca_file_the_os_store_decides(
        self, relay, certificates, monkeypatch
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(certificates["ca"]))
        server = relay()
        assert send(notifier(monkeypatch, server)) is True

    def test_explicit_trust_replaces_the_default_store(
        self, relay, certificates, monkeypatch, caplog, capfd
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(certificates["ca"]))
        server = relay()
        assert send(notifier(monkeypatch, server, certificates["self"][0])) is False
        assert server.done.wait(4)
        assert server.verbs == ["EHLO", "STARTTLS"]
        assert_safe(caplog, capfd, expected_class="SSLCertVerificationError")

    def test_a_directory_os_store_still_delivers(
        self, relay, certificates, monkeypatch, tmp_path
    ):
        # OpenSSL's subject hash is required for lazy directory lookup.
        import subprocess

        result = subprocess.run(
            ["openssl", "x509", "-hash", "-noout", "-in", str(certificates["ca"])],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        (tmp_path / (result.stdout.strip() + ".0")).write_bytes(
            certificates["ca"].read_bytes()
        )
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
        assert email._smtp_tls_context("").cert_store_stats()["x509"] == 0
        server = relay()
        assert send(notifier(monkeypatch, server)) is True

    @pytest.mark.parametrize("label", ["wrong", "expired", "future"])
    def test_a_trusted_certificate_for_another_name_is_refused(
        self, relay, certificates, monkeypatch, caplog, capfd, label
    ):
        server = relay(label)
        assert send(notifier(monkeypatch, server, certificates["ca"])) is False
        assert server.done.wait(4)
        assert server.verbs == ["EHLO", "STARTTLS"]
        assert_safe(caplog, capfd, expected_class="SSLCertVerificationError")

    @pytest.mark.parametrize("user,password", [("", ""), (USER, ""), ("", PASSWORD)])
    @pytest.mark.parametrize("ca", ["", " \t "])
    def test_without_credentials_and_no_ca_file_leaves_the_session_as_it_was(
        self, relay, monkeypatch, user, password, ca
    ):
        server = relay()
        assert send(notifier(monkeypatch, server, ca, user, password)) is True
        assert server.done.wait(4)
        assert server.verbs == ["EHLO", "MAIL", "RCPT", "DATA", "QUIT"]
        assert server.logins == []

    @pytest.mark.parametrize("label,expected", [("valid", True), ("self", False)])
    @pytest.mark.parametrize("user,password", [("", ""), (USER, ""), ("", PASSWORD)])
    def test_a_ca_file_without_a_login_still_verifies_starttls(
        self,
        relay,
        certificates,
        monkeypatch,
        caplog,
        capfd,
        label,
        expected,
        user,
        password,
    ):
        server = relay(label)
        assert (
            send(notifier(monkeypatch, server, certificates["ca"], user, password))
            is expected
        )
        assert server.done.wait(4)
        assert server.logins == []
        assert server.verbs == (
            ["EHLO", "STARTTLS", "EHLO", "MAIL", "RCPT", "DATA", "QUIT"]
            if expected
            else ["EHLO", "STARTTLS"]
        )
        assert_safe(
            caplog,
            capfd,
            expected_class=None if expected else "SSLCertVerificationError",
        )

    @pytest.mark.parametrize(
        "kind", ["missing", "empty", "not-PEM", "directory", "CRL-only"]
    )
    def test_an_unusable_ca_file_sends_nothing(
        self, relay, certificates, monkeypatch, tmp_path, caplog, capfd, kind
    ):
        path = tmp_path / "ca.pem"
        if kind == "empty":
            path.touch()
        elif kind == "not-PEM":
            path.write_text("not a certificate")
        elif kind == "directory":
            path.mkdir()
        elif kind == "CRL-only":
            path.write_bytes(certificates["crl"].read_bytes())
        server = relay()
        instance = notifier(monkeypatch, server, path)
        with patch.object(
            email.smtplib,
            "SMTP",
            side_effect=AssertionError("connection must not open"),
        ) as connect:
            assert send(instance) is False
        connect.assert_not_called()
        assert not server.accepted
        records = assert_safe(caplog, capfd)
        assert len(records) == 1
        assert "SMTP_CA_FILE" in records[0].getMessage()
        assert str(path) in records[0].getMessage()

    def test_a_non_ascii_password_is_not_logged(
        self, relay, certificates, monkeypatch, caplog, capfd
    ):
        server = relay()
        password = "smtp-test-p\u00e4ssword"
        assert (
            send(notifier(monkeypatch, server, certificates["ca"], password=password))
            is False
        )
        records = assert_safe(
            caplog, capfd, expected_class="UnicodeEncodeError", password=password
        )
        assert records[0].smtp_step == "login"
        assert "\\xe4" not in records[0].getMessage()

    @pytest.mark.parametrize(
        "options,expected",
        [
            ({"offer_tls": False, "quit_code": 421}, "SMTPNotSupportedError"),
            ({"reject_tls": True}, "SMTPResponseException"),
            ({"old_tls": True}, "SSLError"),
            ({"break_tls": True}, "SSLError"),
        ],
    )
    def test_a_relay_with_no_starttls_that_errors_on_quit_reports_m2(
        self, relay, certificates, monkeypatch, caplog, capfd, options, expected
    ):
        server = relay(**options)
        assert send(notifier(monkeypatch, server, certificates["ca"])) is False
        assert not server.logins
        assert not server.data_accepted
        records = assert_safe(caplog, capfd, expected_class=expected)
        assert records[0].smtp_step == "starttls"

    @pytest.mark.parametrize("options", [{"login_error": True}, {"data_error": True}])
    def test_server_replies_do_not_enter_logs(
        self, relay, certificates, monkeypatch, caplog, capfd, options
    ):
        server = relay(**options)
        assert send(notifier(monkeypatch, server, certificates["ca"])) is False
        assert_safe(
            caplog,
            capfd,
            expected_class="SMTPAuthenticationError"
            if "login_error" in options
            else "SMTPDataError",
        )

    def test_quit_after_data_reports_uncertain_delivery(
        self, relay, certificates, monkeypatch, caplog, capfd
    ):
        server = relay(quit_code=421)
        assert send(notifier(monkeypatch, server, certificates["ca"])) is False
        assert server.data_accepted
        records = assert_safe(caplog, capfd, expected_class="SMTPResponseException")
        assert records[0].smtp_step == "quit"
        assert "confirm delivery before retrying" in records[0].getMessage()

    def test_ca_loading_occurs_at_send_time(
        self, relay, monkeypatch, tmp_path, caplog, capfd
    ):
        from backend.app.services.notification_service import NotificationService

        server = relay()
        notifier(monkeypatch, server, tmp_path / "missing.pem")
        instance = NotificationService()
        assert isinstance(instance._email, email.EmailNotifier)
        assert (
            instance._email._send_email(["recipient@example.com"], "Test", "Test")
            is False
        )
        assert_safe(caplog, capfd, expected_class="FileNotFoundError")
        assert (
            len(
                [
                    r
                    for r in caplog.records
                    if r.name == LOGGER and r.levelno == logging.WARNING
                ]
            )
            == 1
        )

    def test_no_setting_disables_required_verification(self):
        fields = {
            name
            for name in Settings.model_fields
            if name.startswith(("SMTP_", "EMAIL_"))
        }
        assert fields == {
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "SMTP_CA_FILE",
            "EMAIL_ENABLED",
            "EMAIL_FROM_ADDRESS",
            "EMAIL_FROM_NAME",
        }
        context = email._smtp_tls_context("")
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

    def test_test_route_reports_refused_delivery(
        self, relay, monkeypatch, tmp_path, caplog, capfd
    ):
        from fastapi.testclient import TestClient

        from backend.app.api import deps
        from backend.app.main import app
        from backend.app.models.user import UserRole

        # App initialization may configure root handlers during an isolated run.
        logging.getLogger().addHandler(caplog.handler)
        server = relay()
        notifier(monkeypatch, server, tmp_path / "missing.pem")
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[deps.get_current_active_user] = lambda: (
            SimpleNamespace(role=UserRole.DEVELOPER)
        )
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/notifications/test",
                    json={"channel": "email", "recipient": "recipient@example.com"},
                )
            assert response.status_code == 200
            assert response.json()["success"] is False
            assert response.json()["channel"] == "email"
            assert not server.accepted
            assert_safe(caplog, capfd, expected_class="FileNotFoundError")
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_configuration_values_stay_on_one_log_line(
        self, relay, monkeypatch, caplog, capfd
    ):
        server = relay()
        assert send(notifier(monkeypatch, server, "missing\nsecond-line.pem")) is False
        records = assert_safe(caplog, capfd, expected_class="FileNotFoundError")
        assert "\n" not in records[0].getMessage()
        assert "\\n" in records[0].getMessage()

    def test_file_has_no_optional_execution_mark(self):
        tree = ast.parse(Path(__file__).read_text())
        decorators = [
            ast.unparse(d)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            for d in node.decorator_list
        ]
        assert not any(
            name in decorator
            for decorator in decorators
            for name in ("pytest.mark.skip", "pytest.mark.xfail", "pytest.mark.modules")
        )
