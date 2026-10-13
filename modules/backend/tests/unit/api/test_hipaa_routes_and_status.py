"""The HIPAA module's audit log writers, settings and readiness status (#1142).

Two routes write ``phi_audit_logs``: ``POST /hipaa/audit-logs`` and
``POST /hipaa/decrypt`` (a READ by the caller). ``HIPAA_ENABLED`` and
``HIPAA_ALLOWED_REGIONS`` are reported by ``/data-residency`` and change
nothing else. ``/status`` reads the BAA table and the settings, nothing more.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.user import UserRole
from modules.backend.app.api.v1.endpoints import hipaa as endpoint
from modules.backend.app.models.baa_config import BAAConfig
from modules.backend.app.schemas.hipaa import PHIDecryptRequest
from modules.backend.app.services.hipaa_service import HIPAAService
from modules.backend.app.settings import ModulesSettings

REPO = pathlib.Path(__file__).resolve().parents[5]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), role=UserRole.ADMIN, is_superuser=False)


def _callers(name):
    found = set()
    for tree in TREES:
        for path in tree.rglob("*.py"):
            module = ast.parse(path.read_text())
            for fn in ast.walk(module):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, (ast.Attribute, ast.Name))
                        and getattr(node.func, "attr", getattr(node.func, "id", None))
                        == name
                    ):
                        found.add((path.relative_to(REPO).as_posix(), fn.name))
    return found


def test_two_routes_write_the_phi_audit_log_and_decrypt_records_a_read():
    endpoints = "modules/backend/app/api/v1/endpoints/hipaa.py"
    assert _callers("log_phi_access") == {
        (endpoints, "create_audit_log"),
        (endpoints, "decrypt_phi"),
    }
    assert _callers("PHIAuditLog") == {
        ("modules/backend/app/services/hipaa_service.py", "log_phi_access")
    }

    admin = _admin()
    with (
        patch.object(HIPAAService, "decrypt_phi_field", return_value="plain"),
        patch.object(HIPAAService, "log_phi_access") as log,
    ):
        endpoint.decrypt_phi(
            PHIDecryptRequest(ciphertext="c", field="email"),
            request=MagicMock(),
            current_user=admin,
            db=MagicMock(),
        )
    kwargs = log.call_args.kwargs
    assert (kwargs["action"], kwargs["user_id"]) == ("READ", admin.id)


def test_the_hipaa_settings_are_reported_and_change_nothing_else():
    defaults = ModulesSettings(_env_file=None)
    assert defaults.HIPAA_ALLOWED_REGIONS == ["us-east-1", "us-west-2"]
    assert defaults.HIPAA_ENABLED is False

    readers = set()
    for tree in TREES:
        for path in tree.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Attribute) and node.attr == "HIPAA_ENABLED":
                    readers.add(path.relative_to(REPO).as_posix())
    assert readers == {"modules/backend/app/api/v1/endpoints/hipaa.py"}

    modules_settings = SimpleNamespace(
        HIPAA_ALLOWED_REGIONS=["eu-west-1"], HIPAA_ENABLED=True
    )
    with (
        patch("modules.backend.app.settings.settings", modules_settings),
        patch("backend.app.core.config.settings.AWS_REGION", "eu-central-1"),
    ):
        residency = endpoint.get_data_residency(current_user=_admin())
    assert residency.allowed_regions == ["eu-west-1"]
    assert residency.current_region == "eu-central-1"
    assert residency.hipaa_enabled is True


@pytest.mark.parametrize(
    "baas, key, regions, ready",
    [
        (1, "k", ["us-east-1"], True),
        (0, "k", ["us-east-1"], False),
        (1, None, ["us-east-1"], False),
        (1, "k", [], False),
    ],
)
def test_ready_needs_an_active_baa_a_key_and_an_allowed_region(
    baas, key, regions, ready
):
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = baas
    configured = SimpleNamespace(PHI_ENCRYPTION_KEY=key, HIPAA_ALLOWED_REGIONS=regions)
    with patch("modules.backend.app.services.hipaa_service.settings", configured):
        status = HIPAAService().get_hipaa_status(db)
    assert status["overall_hipaa_ready"] is ready
    # Configuration only: the one table it reads is the BAAs'.
    assert [c.args for c in db.query.call_args_list] == [(BAAConfig,)]
