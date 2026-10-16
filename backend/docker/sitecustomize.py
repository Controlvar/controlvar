"""Start-up hook of the API image's venv (#1265).

backend/Dockerfile copies this file into /opt/venv's site-packages, so every
Python process the venv runs imports it before any user code: the server, a
seed, and a ``docker compose exec api python ...`` alike.

backend/docker-entrypoint.sh probes cryptography's OpenSSL once per start.
When the import dies with SIGILL (an arm64 VM advertising CPU features it
cannot execute, such as Docker Desktop on an Apple M4), the entrypoint exports
OPENSSL_armcap=0 for its own children and leaves a marker file. A process
started later with exec does not inherit that export, so this hook sets the
same value when the marker is there.

It only ever sets ``"0"``, never overrides a value already present (an
operator's own mask, or an empty one, is kept), never reads the marker's
contents and prints nothing. With no marker -- every machine whose CPU the
probe did not crash on -- it does nothing.
"""

import os

if os.path.exists(
    os.path.join(os.environ.get("TMPDIR") or "/tmp", "controlvar-openssl-armcap0")
):
    os.environ.setdefault("OPENSSL_armcap", "0")
