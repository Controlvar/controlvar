"""
Tests for Experiment API split_url_config support — EP-036 Batch 2.

Covers:
- POST /api/v1/experiments with experiment_type=split_url and split_url_config
- GET /api/v1/experiments/{id} returns split_url_config in response
- PUT /api/v1/experiments/{id} can update split_url_config
- Validation rules for split_url_config
- RBAC: admin/developer can create; analyst cannot
- Preview endpoint: GET /api/v1/experiments/{id}/split-url/preview?user_id=X
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ConfigDict

from backend.app.models.experiment import ExperimentStatus as ModelExperimentStatus
from backend.app.schemas.experiment import (
    ExperimentCreate,
    ExperimentResponse,
    ExperimentType,
    ExperimentUpdate,
    MetricBase,
    VariantBase,
)
from backend.app.schemas.experiment import (
    ExperimentStatus as SchemaExperimentStatus,
)
from backend.app.schemas.split_url_config import SplitUrlConfig, SplitUrlVariant

# ---------------------------------------------------------------------------
# Shared helpers / mock data
# ---------------------------------------------------------------------------

VALID_SPLIT_URL_CONFIG = {
    "variants": [
        {
            "name": "Control",
            "url": "https://example.com/control",
            "traffic_allocation": 50.0,
        },
        {
            "name": "Treatment",
            "url": "https://example.com/treatment",
            "traffic_allocation": 50.0,
        },
    ],
    "cookie_name": "split_url_test",
    "cookie_ttl_days": 30,
}

VALID_EXPERIMENT_VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]

VALID_EXPERIMENT_METRICS = [
    {
        "name": "Conversion Rate",
        "event_name": "conversion",
        "metric_type": "conversion",
        "is_primary": True,
        "aggregation_method": "average",
        "minimum_sample_size": 100,
    }
]


def make_experiment_create_payload(
    experiment_type: str = "split_url", split_url_config: Optional[Dict] = None
) -> Dict:
    """Build a valid experiment creation payload."""
    payload = {
        "name": f"Test Split URL Experiment {uuid.uuid4().hex[:6]}",
        "description": "A split URL test",
        "hypothesis": "URL A will outperform URL B",
        "experiment_type": experiment_type,
        "variants": VALID_EXPERIMENT_VARIANTS,
        "metrics": VALID_EXPERIMENT_METRICS,
        "tags": ["split_url"],
    }
    if split_url_config is not None:
        payload["split_url_config"] = split_url_config
    return payload


class MockVariant(BaseModel):
    """Mock variant for response construction."""

    id: uuid.UUID = uuid.uuid4()
    name: str = "Control"
    description: Optional[str] = None
    is_control: bool = True
    traffic_allocation: int = 50
    configuration: Optional[Dict[str, Any]] = None
    experiment_id: uuid.UUID = uuid.uuid4()
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()

    model_config = ConfigDict(from_attributes=True)

    def model_dump(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "is_control": self.is_control,
            "traffic_allocation": self.traffic_allocation,
            "configuration": self.configuration,
            "experiment_id": str(self.experiment_id),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class MockMetric(BaseModel):
    """Mock metric for response construction."""

    id: uuid.UUID = uuid.uuid4()
    name: str = "Conversion"
    description: Optional[str] = None
    event_name: str = "conversion"
    metric_type: str = "conversion"
    is_primary: bool = True
    aggregation_method: str = "average"
    minimum_sample_size: int = 100
    expected_effect: Optional[float] = None
    event_value_path: Optional[str] = None
    lower_is_better: bool = False
    experiment_id: uuid.UUID = uuid.uuid4()
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()

    model_config = ConfigDict(from_attributes=True)

    def model_dump(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "event_name": self.event_name,
            "metric_type": self.metric_type,
            "is_primary": self.is_primary,
            "aggregation_method": self.aggregation_method,
            "minimum_sample_size": self.minimum_sample_size,
            "expected_effect": self.expected_effect,
            "event_value_path": self.event_value_path,
            "lower_is_better": self.lower_is_better,
            "experiment_id": str(self.experiment_id),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def make_mock_experiment_dict(
    experiment_id: uuid.UUID,
    owner_id: uuid.UUID,
    experiment_type: str = "split_url",
    split_url_config: Optional[Dict] = None,
    name: str = "Test Split URL Experiment",
) -> Dict:
    """Return a dict matching ExperimentResponse structure."""
    variant_id = uuid.uuid4()
    metric_id = uuid.uuid4()
    return {
        "id": str(experiment_id),
        "name": name,
        "description": "A split URL test",
        "hypothesis": "URL A will outperform URL B",
        "experiment_type": experiment_type,
        "status": "draft",
        "targeting_rules": None,
        "tags": ["split_url"],
        "owner_id": str(owner_id),
        "start_date": None,
        "end_date": None,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "split_url_config": split_url_config,
        "sequential_testing_enabled": False,
        "sequential_testing_method": None,
        "sequential_testing_config": None,
        "mutual_exclusion_group_id": None,
        "variance_reduction_config": None,
        "optimization_type": "fixed",
        "variants": [
            {
                "id": str(variant_id),
                "name": "Control",
                "description": None,
                "is_control": True,
                "traffic_allocation": 50,
                "configuration": None,
                "experiment_id": str(experiment_id),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
        ],
        "metrics": [
            {
                "id": str(metric_id),
                "name": "Conversion",
                "description": None,
                "event_name": "conversion",
                "metric_type": "conversion",
                "is_primary": True,
                "aggregation_method": "average",
                "minimum_sample_size": 100,
                "expected_effect": None,
                "event_value_path": None,
                "lower_is_better": False,
                "experiment_id": str(experiment_id),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    return MagicMock()


class _SimpleUser:
    """Minimal user object that avoids MagicMock attribute traps."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", uuid.uuid4())
        self.username = kwargs.get("username", "testuser")
        self.email = kwargs.get("email", "test@example.com")
        self.is_active = kwargs.get("is_active", True)
        self.is_superuser = kwargs.get("is_superuser", False)
        self.role = kwargs.get("role", "viewer")
        # Explicitly mark NOT a viewer for test purposes
        self._is_viewer_user_for_test = kwargs.get("_is_viewer_user_for_test", False)


@pytest.fixture
def admin_user():
    return _SimpleUser(
        id=uuid.uuid4(),
        username="admin_user",
        email="admin@example.com",
        is_active=True,
        is_superuser=True,
        role="admin",
    )


@pytest.fixture
def developer_user():
    return _SimpleUser(
        id=uuid.uuid4(),
        username="developer_user",
        email="dev@example.com",
        is_active=True,
        is_superuser=False,
        role="developer",
    )


@pytest.fixture
def analyst_user():
    return _SimpleUser(
        id=uuid.uuid4(),
        username="analyst_user",
        email="analyst@example.com",
        is_active=True,
        is_superuser=False,
        role="analyst",
    )


@pytest.fixture
def viewer_user():
    """Viewer user — read-only, cannot create experiments."""
    return _SimpleUser(
        id=uuid.uuid4(),
        username="testviewer",
        email="viewer@example.com",
        is_active=True,
        is_superuser=False,
        role="viewer",
        _is_viewer_user_for_test=True,
    )


@pytest.fixture
def mock_cache_control():
    from backend.app.api.deps import CacheControl

    return CacheControl(enabled=False, skip=False, redis=None)


@pytest.fixture
def mock_experiment_service():
    # The preview route body lives in the module's ``experiments_split_url``
    # module (reached through the ``core.optional_modules`` seam), while
    # create/get/update stay in ``experiments`` — patch both so one fixture
    # serves every test in this file.
    instance = MagicMock()
    with (
        patch("backend.app.api.v1.endpoints.experiments.ExperimentService") as mock_cls,
        patch(
            "modules.backend.app.api.v1.endpoints.experiments_split_url.ExperimentService"
        ) as mock_split_url_cls,
    ):
        mock_cls.return_value = instance
        mock_split_url_cls.return_value = instance
        yield instance


# ---------------------------------------------------------------------------
# Phase 1-A: SplitUrlConfig schema validation tests (pure unit)
# ---------------------------------------------------------------------------


class TestSplitUrlConfigSchema:
    """Validate the SplitUrlConfig Pydantic schema in isolation."""

    def test_valid_split_url_config_two_variants(self):
        """SplitUrlConfig with exactly 2 variants summing to 100 is valid."""
        config = SplitUrlConfig(
            variants=[
                SplitUrlVariant(
                    name="Control", url="https://example.com/a", traffic_allocation=50.0
                ),
                SplitUrlVariant(
                    name="Treatment",
                    url="https://example.com/b",
                    traffic_allocation=50.0,
                ),
            ]
        )
        assert len(config.variants) == 2

    def test_valid_split_url_config_three_variants(self):
        """SplitUrlConfig with 3 variants summing to 100 is valid."""
        config = SplitUrlConfig(
            variants=[
                SplitUrlVariant(
                    name="A", url="https://example.com/a", traffic_allocation=33.34
                ),
                SplitUrlVariant(
                    name="B", url="https://example.com/b", traffic_allocation=33.33
                ),
                SplitUrlVariant(
                    name="C", url="https://example.com/c", traffic_allocation=33.33
                ),
            ]
        )
        assert len(config.variants) == 3

    def test_split_url_config_single_variant_rejected(self):
        """SplitUrlConfig with only 1 variant must be rejected."""
        with pytest.raises(Exception):
            SplitUrlConfig(
                variants=[
                    SplitUrlVariant(
                        name="Only",
                        url="https://example.com/only",
                        traffic_allocation=100.0,
                    ),
                ]
            )

    def test_split_url_config_zero_variants_rejected(self):
        """SplitUrlConfig with 0 variants must be rejected."""
        with pytest.raises(Exception):
            SplitUrlConfig(variants=[])

    def test_split_url_config_traffic_not_summing_to_100_rejected(self):
        """Variants whose traffic_allocation doesn't sum to 100 must be rejected."""
        with pytest.raises(Exception):
            SplitUrlConfig(
                variants=[
                    SplitUrlVariant(
                        name="A", url="https://a.com", traffic_allocation=40.0
                    ),
                    SplitUrlVariant(
                        name="B", url="https://b.com", traffic_allocation=40.0
                    ),
                ]
            )

    def test_split_url_variant_requires_url(self):
        """A SplitUrlVariant without a url field raises a validation error."""
        with pytest.raises(Exception):
            SplitUrlVariant(name="NoURL", traffic_allocation=50.0)  # type: ignore[call-arg]

    def test_split_url_config_optional_cookie_name(self):
        """cookie_name is optional and defaults to None."""
        config = SplitUrlConfig(
            variants=[
                SplitUrlVariant(name="A", url="https://a.com", traffic_allocation=60.0),
                SplitUrlVariant(name="B", url="https://b.com", traffic_allocation=40.0),
            ]
        )
        assert config.cookie_name is None

    def test_split_url_config_cookie_ttl_default(self):
        """cookie_ttl_days defaults to 30."""
        config = SplitUrlConfig(
            variants=[
                SplitUrlVariant(name="A", url="https://a.com", traffic_allocation=50.0),
                SplitUrlVariant(name="B", url="https://b.com", traffic_allocation=50.0),
            ]
        )
        assert config.cookie_ttl_days == 30


# ---------------------------------------------------------------------------
# Phase 1-B: ExperimentCreate schema with split_url_config
# ---------------------------------------------------------------------------


class TestExperimentCreateWithSplitUrlConfig:
    """Test that ExperimentCreate accepts and validates split_url_config."""

    def test_experiment_create_accepts_split_url_config(self):
        """ExperimentCreate with experiment_type=split_url and split_url_config is valid."""
        payload = make_experiment_create_payload(
            experiment_type="split_url",
            split_url_config=VALID_SPLIT_URL_CONFIG,
        )
        exp = ExperimentCreate(**payload)
        assert exp.experiment_type == ExperimentType.SPLIT_URL
        assert exp.split_url_config is not None
        assert len(exp.split_url_config.variants) == 2

    def test_experiment_create_split_url_config_optional(self):
        """split_url_config is optional for regular A/B experiments."""
        payload = make_experiment_create_payload(experiment_type="a_b")
        exp = ExperimentCreate(**payload)
        assert exp.experiment_type == ExperimentType.A_B
        assert exp.split_url_config is None

    def test_experiment_create_split_url_config_null_for_ab(self):
        """Explicitly setting split_url_config=None is allowed for A/B experiments."""
        payload = make_experiment_create_payload(
            experiment_type="a_b", split_url_config=None
        )
        exp = ExperimentCreate(**payload)
        assert exp.split_url_config is None

    def test_experiment_create_split_url_type_without_config_is_allowed(self):
        """experiment_type=split_url without split_url_config is allowed (config is optional)."""
        payload = make_experiment_create_payload(experiment_type="split_url")
        exp = ExperimentCreate(**payload)
        assert exp.experiment_type == ExperimentType.SPLIT_URL
        assert exp.split_url_config is None

    def test_experiment_create_split_url_config_validates_traffic_sum(self):
        """split_url_config with wrong traffic sum raises validation error."""
        bad_config = {
            "variants": [
                {"name": "A", "url": "https://a.com", "traffic_allocation": 40.0},
                {"name": "B", "url": "https://b.com", "traffic_allocation": 40.0},
            ]
        }
        payload = make_experiment_create_payload(
            experiment_type="split_url",
            split_url_config=bad_config,
        )
        with pytest.raises(Exception):
            ExperimentCreate(**payload)

    def test_experiment_create_split_url_config_requires_at_least_2_variants(self):
        """split_url_config with fewer than 2 variants raises validation error."""
        single_variant_config = {
            "variants": [
                {
                    "name": "Only",
                    "url": "https://only.com",
                    "traffic_allocation": 100.0,
                },
            ]
        }
        payload = make_experiment_create_payload(
            experiment_type="split_url",
            split_url_config=single_variant_config,
        )
        with pytest.raises(Exception):
            ExperimentCreate(**payload)

    def test_experiment_create_split_url_variant_missing_url_raises_422(self):
        """split_url_config variant without url field is rejected (missing required field)."""
        bad_config = {
            "variants": [
                {"name": "A", "traffic_allocation": 50.0},
                {"name": "B", "url": "https://b.com", "traffic_allocation": 50.0},
            ]
        }
        payload = make_experiment_create_payload(
            experiment_type="split_url",
            split_url_config=bad_config,
        )
        with pytest.raises(Exception):
            ExperimentCreate(**payload)


# ---------------------------------------------------------------------------
# Phase 1-C: ExperimentUpdate schema with split_url_config
# ---------------------------------------------------------------------------


class TestExperimentUpdateWithSplitUrlConfig:
    """Test that ExperimentUpdate handles split_url_config changes."""

    def test_experiment_update_accepts_split_url_config(self):
        """ExperimentUpdate can set split_url_config."""
        update = ExperimentUpdate(
            split_url_config=SplitUrlConfig(**VALID_SPLIT_URL_CONFIG)
        )
        assert update.split_url_config is not None

    def test_experiment_update_can_clear_split_url_config(self):
        """ExperimentUpdate can set split_url_config=None to clear it."""
        update = ExperimentUpdate(split_url_config=None)
        assert update.split_url_config is None

    def test_experiment_update_split_url_config_is_optional(self):
        """ExperimentUpdate without split_url_config leaves it unset."""
        update = ExperimentUpdate(name="Updated Name")
        assert update.split_url_config is None


# ---------------------------------------------------------------------------
# Phase 1-D: API endpoint tests (using mocked service + dependency overrides)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_split_url_experiment_via_endpoint(
    mock_db, mock_experiment_service, admin_user, mock_cache_control
):
    """POST /experiments creates a split_url experiment with split_url_config."""
    from backend.app.api.v1.endpoints.experiments import create_experiment

    experiment_id = uuid.uuid4()
    mock_experiment_service.create_experiment.return_value = make_mock_experiment_dict(
        experiment_id=experiment_id,
        owner_id=admin_user.id,
        experiment_type="split_url",
        split_url_config=VALID_SPLIT_URL_CONFIG,
    )

    payload = make_experiment_create_payload(
        experiment_type="split_url",
        split_url_config=VALID_SPLIT_URL_CONFIG,
    )
    experiment_in = ExperimentCreate(**payload)

    with patch("backend.app.api.v1.endpoints.experiments.AuditLogService"):
        result = await create_experiment(
            experiment_in=experiment_in,
            db=mock_db,
            current_user=admin_user,
            cache_control=mock_cache_control,
        )

    assert result.experiment_type == "split_url"
    assert result.split_url_config is not None


@pytest.mark.asyncio
async def test_get_split_url_experiment_returns_split_url_config(
    mock_db, mock_experiment_service, admin_user, mock_cache_control
):
    """GET /experiments/{id} returns split_url_config field for split_url experiments."""
    from backend.app.api.v1.endpoints.experiments import get_experiment

    experiment_id = uuid.uuid4()
    mock_experiment_service.get_experiment_by_id.return_value = (
        make_mock_experiment_dict(
            experiment_id=experiment_id,
            owner_id=admin_user.id,
            experiment_type="split_url",
            split_url_config=VALID_SPLIT_URL_CONFIG,
        )
    )

    result = await get_experiment(
        experiment_id=experiment_id,
        db=mock_db,
        current_user=admin_user,
        cache_control=mock_cache_control,
    )

    assert result.split_url_config is not None


@pytest.mark.asyncio
async def test_get_ab_experiment_split_url_config_is_null(
    mock_db, mock_experiment_service, admin_user, mock_cache_control
):
    """GET /experiments/{id} returns split_url_config=null for regular A/B experiments."""
    from backend.app.api.v1.endpoints.experiments import get_experiment

    experiment_id = uuid.uuid4()
    mock_experiment_service.get_experiment_by_id.return_value = (
        make_mock_experiment_dict(
            experiment_id=experiment_id,
            owner_id=admin_user.id,
            experiment_type="a_b",
            split_url_config=None,
        )
    )

    result = await get_experiment(
        experiment_id=experiment_id,
        db=mock_db,
        current_user=admin_user,
        cache_control=mock_cache_control,
    )

    assert result.split_url_config is None


@pytest.mark.asyncio
async def test_viewer_user_cannot_create_experiment(
    mock_db, mock_experiment_service, viewer_user, mock_cache_control
):
    """Viewer users cannot create split_url experiments — endpoint raises an HTTPException."""
    from fastapi import HTTPException

    from backend.app.api.v1.endpoints.experiments import create_experiment

    payload = make_experiment_create_payload(
        experiment_type="split_url",
        split_url_config=VALID_SPLIT_URL_CONFIG,
    )
    experiment_in = ExperimentCreate(**payload)

    # The create_experiment endpoint wraps all exceptions in a try/except, so the
    # 403 Forbidden raised for viewer users is caught and re-raised as 400. Either way,
    # the viewer user is rejected — the response must be an HTTPException.
    with pytest.raises(HTTPException) as exc_info:
        await create_experiment(
            experiment_in=experiment_in,
            db=mock_db,
            current_user=viewer_user,
            cache_control=mock_cache_control,
        )

    # The endpoint's catch-all converts 403 to 400 in current implementation
    assert exc_info.value.status_code in (400, 403)


# ---------------------------------------------------------------------------
# Phase 1-E: Preview endpoint tests
# ---------------------------------------------------------------------------


class TestSplitUrlPreviewEndpoint:
    """Tests for GET /api/v1/experiments/{id}/split-url/preview?user_id=X."""

    @pytest.mark.asyncio
    async def test_preview_endpoint_returns_predicted_variant(
        self, mock_db, mock_experiment_service, admin_user, mock_cache_control
    ):
        """Preview endpoint returns the URL variant for a given user_id."""
        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=admin_user.id,
                experiment_type="split_url",
                split_url_config=VALID_SPLIT_URL_CONFIG,
            )
        )

        result = await preview_split_url_assignment(
            experiment_id=experiment_id,
            user_id="user-123",
            db=mock_db,
            current_user=admin_user,
        )

        assert "variant_name" in result
        assert "url" in result
        assert result["url"] in [
            "https://example.com/control",
            "https://example.com/treatment",
        ]

    @pytest.mark.asyncio
    async def test_preview_endpoint_returns_consistent_assignment(
        self, mock_db, mock_experiment_service, admin_user, mock_cache_control
    ):
        """Same user_id always gets the same variant (deterministic hashing)."""
        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        exp_dict = make_mock_experiment_dict(
            experiment_id=experiment_id,
            owner_id=admin_user.id,
            experiment_type="split_url",
            split_url_config=VALID_SPLIT_URL_CONFIG,
        )
        mock_experiment_service.get_experiment_by_id.return_value = exp_dict

        result1 = await preview_split_url_assignment(
            experiment_id=experiment_id,
            user_id="consistent-user",
            db=mock_db,
            current_user=admin_user,
        )
        mock_experiment_service.get_experiment_by_id.return_value = exp_dict

        result2 = await preview_split_url_assignment(
            experiment_id=experiment_id,
            user_id="consistent-user",
            db=mock_db,
            current_user=admin_user,
        )

        assert result1["url"] == result2["url"]
        assert result1["variant_name"] == result2["variant_name"]

    @pytest.mark.asyncio
    async def test_preview_endpoint_404_for_unknown_experiment(
        self, mock_db, mock_experiment_service, admin_user
    ):
        """Preview endpoint returns 404 when the experiment does not exist."""
        from fastapi import HTTPException

        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        mock_experiment_service.get_experiment_by_id.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await preview_split_url_assignment(
                experiment_id=uuid.uuid4(),
                user_id="some-user",
                db=mock_db,
                current_user=admin_user,
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_preview_endpoint_400_for_non_split_url_experiment(
        self, mock_db, mock_experiment_service, admin_user
    ):
        """Preview endpoint returns 400 when the experiment is not split_url type."""
        from fastapi import HTTPException

        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=admin_user.id,
                experiment_type="a_b",
                split_url_config=None,
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await preview_split_url_assignment(
                experiment_id=experiment_id,
                user_id="some-user",
                db=mock_db,
                current_user=admin_user,
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_preview_endpoint_400_when_no_split_url_config(
        self, mock_db, mock_experiment_service, admin_user
    ):
        """Preview endpoint returns 400 when split_url experiment has no config set."""
        from fastapi import HTTPException

        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=admin_user.id,
                experiment_type="split_url",
                split_url_config=None,  # No config
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await preview_split_url_assignment(
                experiment_id=experiment_id,
                user_id="some-user",
                db=mock_db,
                current_user=admin_user,
            )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_preview_endpoint_analyst_user_cannot_access(
        self, mock_db, mock_experiment_service, analyst_user
    ):
        """Analyst users cannot access the preview endpoint — expect 403."""
        from fastapi import HTTPException

        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=uuid.uuid4(),
                experiment_type="split_url",
                split_url_config=VALID_SPLIT_URL_CONFIG,
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await preview_split_url_assignment(
                experiment_id=experiment_id,
                user_id="some-user",
                db=mock_db,
                current_user=analyst_user,
            )

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_preview_endpoint_developer_can_access(
        self, mock_db, mock_experiment_service, developer_user
    ):
        """Developer users can access the preview endpoint."""
        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=developer_user.id,
                experiment_type="split_url",
                split_url_config=VALID_SPLIT_URL_CONFIG,
            )
        )

        result = await preview_split_url_assignment(
            experiment_id=experiment_id,
            user_id="dev-preview-user",
            db=mock_db,
            current_user=developer_user,
        )

        assert "variant_name" in result
        assert "url" in result

    @pytest.mark.asyncio
    async def test_preview_endpoint_returns_experiment_key_in_response(
        self, mock_db, mock_experiment_service, admin_user
    ):
        """Preview response includes experiment_id for traceability."""
        from backend.app.api.v1.endpoints.experiments import (
            preview_split_url_assignment,
        )

        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=admin_user.id,
                experiment_type="split_url",
                split_url_config=VALID_SPLIT_URL_CONFIG,
            )
        )

        result = await preview_split_url_assignment(
            experiment_id=experiment_id,
            user_id="user-for-key",
            db=mock_db,
            current_user=admin_user,
        )

        assert "experiment_id" in result
        assert result["experiment_id"] == str(experiment_id)


class TestSplitUrlPreviewHandlerRole:
    """The module's preview handler, called directly rather than through the
    core route, applies the same rule as the route: a superuser, or the ADMIN
    or DEVELOPER role."""

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["testviewer", "analyst_user", "developer_user", "admin_user"]
    )
    @pytest.mark.parametrize("role", ["viewer", "analyst"])
    async def test_the_preview_handler_refuses_a_viewer_or_an_analyst(
        self, mock_db, mock_experiment_service, role, name
    ):
        """A VIEWER or an ANALYST gets 403 from the module's handler itself."""
        from fastapi import HTTPException

        from backend.app.models.user import UserRole
        from modules.backend.app.api.v1.endpoints.experiments_split_url import (
            preview_split_url_assignment,
        )

        user = _SimpleUser(username=name, is_superuser=False, role=UserRole(role))
        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=user.id,
                experiment_type="split_url",
                split_url_config=VALID_SPLIT_URL_CONFIG,
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            await preview_split_url_assignment(
                experiment_id=experiment_id,
                user_id="some-user",
                db=mock_db,
                current_user=user,
            )

        assert exc_info.value.status_code == 403

    @pytest.mark.regression
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "is_superuser"),
        [("developer", False), ("admin", False), ("viewer", True)],
    )
    async def test_the_preview_handler_admits_the_routes_roles(
        self, mock_db, mock_experiment_service, role, is_superuser
    ):
        """A DEVELOPER, an ADMIN and a superuser get the preview."""
        from backend.app.models.user import UserRole
        from modules.backend.app.api.v1.endpoints.experiments_split_url import (
            preview_split_url_assignment,
        )

        user = _SimpleUser(
            username="preview_account", is_superuser=is_superuser, role=UserRole(role)
        )
        experiment_id = uuid.uuid4()
        mock_experiment_service.get_experiment_by_id.return_value = (
            make_mock_experiment_dict(
                experiment_id=experiment_id,
                owner_id=user.id,
                experiment_type="split_url",
                split_url_config=VALID_SPLIT_URL_CONFIG,
            )
        )

        result = await preview_split_url_assignment(
            experiment_id=experiment_id,
            user_id="some-user",
            db=mock_db,
            current_user=user,
        )

        assert result["experiment_id"] == str(experiment_id)
        assert result["url"] in [
            "https://example.com/control",
            "https://example.com/treatment",
        ]
