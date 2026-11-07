"""A Cognito Hosted UI token endpoint, for the tests of the code-exchange relay.

Not a test module. :class:`FakeIssuer` serves ``POST /oauth2/token`` on
127.0.0.1 in a thread, as AWS documents the Hosted UI's token endpoint
(https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html):
a form body with ``grant_type=authorization_code``, a code it issued, the
``client_id`` and ``redirect_uri`` the code was issued for, and the PKCE
``code_verifier`` whose S256 hash is the code's challenge. It answers JSON:
``access_token``, ``id_token``, ``refresh_token``, ``expires_in`` and
``token_type``, or HTTP 400 with ``{"error": ...}``. A code is good once.

The relay builds ``https://<COGNITO_DOMAIN>/oauth2/token``. :meth:`transport`
sends that request to this server instead and changes nothing else: the
``Host`` header is still the one the relay chose, so :attr:`requests` records
the domain and client the relay asked for, and the timeouts are httpx's own
over a real socket. The fake is written from AWS's documentation, so it can
agree with a misreading of it; a real user pool is what checks the endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import httpx

TOKEN_PATH = "/oauth2/token"

#: (HTTP status, body bytes, content type) the fake answers every request with.
Answer = Tuple[int, bytes, str]


def s256(verifier: str) -> str:
    """The PKCE S256 challenge of ``verifier`` (RFC 7636 section 4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    """A PKCE code verifier: 86 characters of the unreserved set."""
    return secrets.token_urlsafe(64)


def json_answer(status: int, body: Dict[str, Any]) -> Answer:
    return status, json.dumps(body).encode("utf-8"), "application/json"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that gave up (the relay's timeout) leaves a broken pipe.
        pass


class _ToFakeIssuer(httpx.HTTPTransport):
    """Sends every request to 127.0.0.1:``port`` over plain HTTP, as it is.

    Only the request's URL changes; its headers, ``Host`` included, are the
    ones the relay built for the URL it chose.
    """

    def __init__(self, port: int) -> None:
        super().__init__()
        self._port = port

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme="http", host="127.0.0.1", port=self._port
        )
        return super().handle_request(request)


class FakeIssuer:
    """The token endpoint of a Hosted UI that has signed one or more users in.

    ``issue_code`` stands for a sign-in finished on the Hosted UI (a password,
    an MFA step, a new password): it returns the code Cognito would send to
    the callback. ``answer`` replaces every answer with a fixed one, and
    ``delay`` holds each answer back for that many seconds.
    """

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.answers: List[int] = []
        self.answer: Optional[Answer] = None
        self.delay: float = 0.0
        self._codes: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "FakeIssuer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def transport(self) -> httpx.BaseTransport:
        return _ToFakeIssuer(self.port)

    @staticmethod
    def unreachable_transport() -> httpx.BaseTransport:
        """A transport to a local port nothing listens on."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return _ToFakeIssuer(port)

    # -- the Hosted UI ---------------------------------------------------

    def issue_code(
        self,
        tokens: Dict[str, Any],
        *,
        verifier: str,
        client_id: str,
        redirect_uri: str,
    ) -> str:
        """A code for ``tokens`` (Cognito's ``AuthenticationResult``)."""
        code = secrets.token_hex(18)
        with self._lock:
            self._codes[code] = {
                "challenge": s256(verifier),
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "tokens": tokens,
            }
        return code

    def _exchange(self, form: Dict[str, str]) -> Answer:
        if form.get("grant_type") != "authorization_code":
            return json_answer(400, {"error": "unsupported_grant_type"})
        with self._lock:
            pending = self._codes.pop(form.get("code", ""), None)
        if pending is None:
            return json_answer(400, {"error": "invalid_grant"})
        if form.get("client_id") != pending["client_id"]:
            return json_answer(400, {"error": "invalid_client"})
        if form.get("redirect_uri") != pending["redirect_uri"]:
            return json_answer(400, {"error": "invalid_grant"})
        if s256(form.get("code_verifier", "")) != pending["challenge"]:
            return json_answer(400, {"error": "invalid_grant"})
        tokens = pending["tokens"]
        return json_answer(
            200,
            {
                "access_token": tokens["AccessToken"],
                "id_token": tokens["IdToken"],
                "refresh_token": tokens["RefreshToken"],
                "expires_in": tokens.get("ExpiresIn", 3600),
                "token_type": "Bearer",
            },
        )

    def _handler(self):
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                with issuer._lock:
                    issuer.requests.append(
                        {
                            "host": self.headers.get("Host"),
                            "path": self.path,
                            "content_type": self.headers.get("Content-Type"),
                            "form": form,
                        }
                    )
                if issuer.delay:
                    time.sleep(issuer.delay)
                if self.path != TOKEN_PATH:
                    answer = json_answer(404, {"error": "not_found"})
                elif issuer.answer is not None:
                    answer = issuer.answer
                else:
                    answer = issuer._exchange(form)
                with issuer._lock:
                    issuer.answers.append(answer[0])
                status, body, content_type = answer
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
