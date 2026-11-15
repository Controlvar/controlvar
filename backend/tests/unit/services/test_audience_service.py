"""
Unit tests for AudienceService.

Uses MagicMock for DB session; membership runs the real flag engine.
No real database required.
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from backend.app.models.segment import Segment
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.app.schemas.segment import (
    AudiencePreviewResponse,
    BulkSegmentMembershipRequest,
    BulkSegmentMembershipResponse,
    SegmentCreate,
    SegmentMembershipResponse,
    SegmentStatus,
    SegmentUpdate,
)
from backend.app.services import audience_service
from backend.app.services.audience_service import (
    AudienceService,
    SegmentRulesNotValid,
)


def _rules(*conditions, logical_operator="AND"):
    """Segment rules in the targeting rule format (the dashboard shape)."""
    return {
        "logical_operator": logical_operator,
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


def _eq(attribute, value):
    return {"attribute": attribute, "operator": "equals", "value": value}


US_RULES = _rules(_eq("country", "US"))

#: A segment: US users, or a time_window the engine cannot read (ISO datetimes).
UNREADABLE_WINDOW_RULES = {
    "logical_operator": "OR",
    "groups": [
        {"logical_operator": "AND", "conditions": [_eq("country", "US")]},
        {
            "logical_operator": "NOT",
            "conditions": [
                {
                    "attribute": "now",
                    "operator": "time_window",
                    "value": {
                        "start": "2024-01-01T00:00:00",
                        "end": "2024-12-31T00:00:00",
                    },
                }
            ],
        },
    ],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_segment(
    segment_id=None,
    name="Test Segment",
    status=ModelSegmentStatus.ACTIVE,
    rules=None,
):
    """Create a mock Segment object."""
    seg = MagicMock(spec=Segment)
    seg.id = segment_id or uuid.uuid4()
    seg.name = name
    seg.description = "Test description"
    seg.status = status
    seg.rules = US_RULES if rules is None else rules
    seg.created_at = datetime(2025, 1, 1, 12, 0)
    seg.updated_at = datetime(2025, 1, 2, 12, 0)
    return seg


def _make_mock_db(segment=None):
    """Create a mock DB session that returns the given segment from queries."""
    db = MagicMock()
    query_chain = db.query.return_value.filter.return_value
    query_chain.first.return_value = segment
    query_chain.offset.return_value.limit.return_value.all.return_value = (
        [segment] if segment else []
    )
    return db


# ---------------------------------------------------------------------------
# create_segment
# ---------------------------------------------------------------------------


class TestCreateSegment:
    def test_create_segment_persists_with_correct_fields(self):
        """create_segment adds segment to session and commits."""
        db = MagicMock()
        data = SegmentCreate(
            name="My Segment",
            rules=US_RULES,
        )
        user_id = uuid.uuid4()

        # Simulate db.refresh side effect: set id and timestamps on the segment
        def fake_refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = datetime(2025, 1, 1)
            obj.updated_at = datetime(2025, 1, 1)
            obj.status = ModelSegmentStatus.ACTIVE

        db.refresh.side_effect = fake_refresh

        segment = AudienceService.create_segment(db, data, created_by_id=user_id)

        db.add.assert_called_once()
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

        added_segment = db.add.call_args[0][0]
        assert added_segment.name == "My Segment"
        assert added_segment.owner_id == user_id

    def test_create_segment_sets_active_status_by_default(self):
        """New segments are created with ACTIVE status."""
        db = MagicMock()
        db.refresh.side_effect = lambda obj: None
        data = SegmentCreate(
            name="Status Segment",
            rules=US_RULES,
        )

        AudienceService.create_segment(db, data)

        added_segment = db.add.call_args[0][0]
        assert added_segment.status == ModelSegmentStatus.ACTIVE

    def test_create_segment_stores_rules_as_provided(self):
        """Segment rules are stored exactly as provided in SegmentCreate."""
        db = MagicMock()
        db.refresh.side_effect = lambda obj: None
        rules = _rules(_eq("plan", "premium"), logical_operator="OR")
        data = SegmentCreate(name="Rules Segment", rules=rules)

        AudienceService.create_segment(db, data)

        added_segment = db.add.call_args[0][0]
        assert added_segment.rules == rules


# ---------------------------------------------------------------------------
# list_segments
# ---------------------------------------------------------------------------


class TestListSegments:
    def test_list_segments_returns_all_when_no_status_filter(self):
        """list_segments without status filter queries all segments."""
        db = MagicMock()
        mock_seg = _make_mock_segment()
        db.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = [
            mock_seg
        ]

        results = AudienceService.list_segments(db)
        assert len(results) == 1

    def test_list_segments_filters_by_status(self):
        """list_segments with status filter applies filter to query."""
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = []

        AudienceService.list_segments(db, status=SegmentStatus.INACTIVE)
        db.query.return_value.filter.assert_called_once()


# ---------------------------------------------------------------------------
# get_segment
# ---------------------------------------------------------------------------


class TestGetSegment:
    def test_get_segment_returns_segment_when_found(self):
        """get_segment returns the segment when it exists."""
        mock_seg = _make_mock_segment()
        db = _make_mock_db(segment=mock_seg)

        result = AudienceService.get_segment(db, str(mock_seg.id))
        assert result is mock_seg

    def test_get_segment_raises_value_error_when_not_found(self):
        """get_segment raises ValueError for non-existent segment."""
        db = _make_mock_db(segment=None)

        with pytest.raises(ValueError, match="not found"):
            AudienceService.get_segment(db, str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# update_segment
# ---------------------------------------------------------------------------


class TestUpdateSegment:
    def test_update_segment_updates_name(self):
        """update_segment changes the name field."""
        mock_seg = _make_mock_segment()
        db = _make_mock_db(segment=mock_seg)
        db.refresh.side_effect = lambda obj: None

        data = SegmentUpdate(name="New Name")
        AudienceService.update_segment(db, str(mock_seg.id), data)

        assert mock_seg.name == "New Name"
        db.commit.assert_called_once()

    def test_update_segment_updates_status(self):
        """update_segment changes the status field."""
        mock_seg = _make_mock_segment()
        db = _make_mock_db(segment=mock_seg)
        db.refresh.side_effect = lambda obj: None

        data = SegmentUpdate(status=SegmentStatus.INACTIVE)
        AudienceService.update_segment(db, str(mock_seg.id), data)

        assert mock_seg.status == ModelSegmentStatus.INACTIVE

    def test_update_segment_raises_when_not_found(self):
        """update_segment raises ValueError for non-existent segment."""
        db = _make_mock_db(segment=None)

        with pytest.raises(ValueError, match="not found"):
            AudienceService.update_segment(
                db, str(uuid.uuid4()), SegmentUpdate(name="ValidName")
            )


# ---------------------------------------------------------------------------
# delete_segment (soft delete)
# ---------------------------------------------------------------------------


class TestDeleteSegment:
    def test_delete_segment_sets_status_to_archived(self):
        """delete_segment performs soft delete by setting status=ARCHIVED."""
        mock_seg = _make_mock_segment()
        db = _make_mock_db(segment=mock_seg)

        AudienceService.delete_segment(db, str(mock_seg.id))

        assert mock_seg.status == ModelSegmentStatus.ARCHIVED
        db.commit.assert_called_once()

    def test_delete_segment_does_not_hard_delete(self):
        """delete_segment never calls db.delete()."""
        mock_seg = _make_mock_segment()
        db = _make_mock_db(segment=mock_seg)

        AudienceService.delete_segment(db, str(mock_seg.id))

        db.delete.assert_not_called()

    def test_delete_segment_raises_when_not_found(self):
        """delete_segment raises ValueError for non-existent segment."""
        db = _make_mock_db(segment=None)

        with pytest.raises(ValueError, match="not found"):
            AudienceService.delete_segment(db, str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# evaluate_membership
# ---------------------------------------------------------------------------


class TestEvaluateMembership:
    def test_evaluate_membership_returns_true_when_rules_match(self):
        """evaluate_membership returns is_member=True when rules match."""
        mock_seg = _make_mock_segment(rules=US_RULES)
        db = _make_mock_db(segment=mock_seg)

        result = AudienceService.evaluate_membership(
            db, str(mock_seg.id), {"country": "US"}
        )

        assert result.is_member is True
        assert result.segment_id == str(mock_seg.id)
        assert result.segment_name == mock_seg.name
        assert result.matched_rules == ["country equals US"]

    def test_matched_rules_lists_each_condition_in_any_group_that_holds_on_its_own(
        self,
    ):
        """A member by the second group: the first group's one condition that
        holds is listed too, and the one that does not is not."""
        rules = {
            "logical_operator": "OR",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [_eq("country", "US"), _eq("plan", "pro")],
                },
                {"logical_operator": "AND", "conditions": [_eq("browser", "chrome")]},
            ],
        }
        mock_seg = _make_mock_segment(rules=rules)
        db = _make_mock_db(segment=mock_seg)

        result = AudienceService.evaluate_membership(
            db, str(mock_seg.id), {"country": "US", "plan": "free", "browser": "chrome"}
        )

        assert result.is_member is True
        assert result.matched_rules == ["country equals US", "browser equals chrome"]

    def test_evaluate_membership_returns_false_when_rules_do_not_match(self):
        """evaluate_membership returns is_member=False when rules don't match."""
        mock_seg = _make_mock_segment(rules=US_RULES)
        db = _make_mock_db(segment=mock_seg)

        result = AudienceService.evaluate_membership(
            db, str(mock_seg.id), {"country": "CA"}
        )

        assert result.is_member is False
        assert result.matched_rules == []

    @pytest.mark.regression
    @pytest.mark.parametrize("country", ["US", "CA"])
    def test_a_time_window_that_cannot_be_read_grants_no_membership(self, country):
        """A ``time_window`` the engine cannot read (ISO datetimes here) makes
        no user a member, whatever the other groups say (#1145)."""
        rules = UNREADABLE_WINDOW_RULES
        mock_seg = _make_mock_segment(rules=rules)
        db = _make_mock_db(segment=mock_seg)

        result = AudienceService.evaluate_membership(
            db, str(mock_seg.id), {"country": country, "now": "2026-01-05T12:00:00"}
        )

        assert result.is_member is False
        assert result.matched_rules == []

    def test_evaluate_membership_raises_value_error_for_non_existent_segment(self):
        """evaluate_membership raises ValueError when segment doesn't exist."""
        db = _make_mock_db(segment=None)

        with pytest.raises(ValueError, match="not found"):
            AudienceService.evaluate_membership(
                db, str(uuid.uuid4()), {"country": "US"}
            )

    @pytest.mark.parametrize(
        "rules",
        [
            {},
            None,
            {"operator": "and", "conditions": [_eq("country", "US")]},
            {"groups": [{"conditions": [{**_eq("country", "US"), "operator": "eq"}]}]},
            {"groups": []},
            {"groups": [{"conditions": []}]},
            {"groups": "x"},
        ],
        ids=["empty", "null", "legacy", "pe-eq", "pe-no-groups", "pe-empty", "pe-x"],
    )
    def test_stored_rules_that_are_not_valid_raise(self, rules):
        """Stored rules that fail validation are never evaluated."""
        mock_seg = _make_mock_segment(rules=rules)
        mock_seg.rules = rules
        db = _make_mock_db(segment=mock_seg)

        with pytest.raises(SegmentRulesNotValid):
            AudienceService.evaluate_membership(db, str(mock_seg.id), {"country": "US"})

    def test_evaluate_membership_returns_instance_of_response(self):
        """evaluate_membership returns a SegmentMembershipResponse."""
        mock_seg = _make_mock_segment()
        db = _make_mock_db(segment=mock_seg)

        result = AudienceService.evaluate_membership(db, str(mock_seg.id), {})

        assert isinstance(result, SegmentMembershipResponse)

    def test_a_text_id_that_is_not_a_uuid_is_not_found_without_a_query(self):
        db = MagicMock()

        with pytest.raises(ValueError, match="not found"):
            AudienceService.get_segment(db, "seg-1")
        db.query.assert_not_called()


# ---------------------------------------------------------------------------
# bulk_evaluate_membership
# ---------------------------------------------------------------------------


class TestBulkEvaluateMembership:
    def test_bulk_evaluate_returns_correct_memberships_dict(self):
        """bulk_evaluate_membership returns correct memberships for each segment."""
        seg1 = _make_mock_segment(rules=US_RULES)
        seg2 = _make_mock_segment(rules=_rules(_eq("country", "DE")))
        seg1_id = str(seg1.id)
        seg2_id = str(seg2.id)

        db = MagicMock()

        with patch.object(
            AudienceService,
            "get_segment",
            side_effect=lambda db, sid: seg1 if sid == seg1_id else seg2,
        ):
            request = BulkSegmentMembershipRequest(
                user_context={"country": "US"},
                segment_ids=[seg1_id, seg2_id],
            )
            result = AudienceService.bulk_evaluate_membership(db, request)

        assert result.memberships[seg1_id] is True
        assert result.memberships[seg2_id] is False

    def test_bulk_evaluate_records_evaluation_time_ms_greater_than_zero(self):
        """bulk_evaluate_membership records a positive evaluation_time_ms."""
        seg = _make_mock_segment(rules=US_RULES)
        seg_id = str(seg.id)
        db = MagicMock()

        with patch.object(AudienceService, "get_segment", return_value=seg):
            request = BulkSegmentMembershipRequest(
                user_context={"country": "US"},
                segment_ids=[seg_id],
            )
            result = AudienceService.bulk_evaluate_membership(db, request)

        assert result.evaluation_time_ms >= 0.0

    def test_bulk_evaluate_handles_missing_segment_gracefully(self):
        """bulk_evaluate treats missing segments as non-member (False)."""
        db = MagicMock()
        missing_id = str(uuid.uuid4())

        with patch.object(
            AudienceService,
            "get_segment",
            side_effect=ValueError("not found"),
        ):
            request = BulkSegmentMembershipRequest(
                user_context={"country": "US"},
                segment_ids=[missing_id],
            )
            result = AudienceService.bulk_evaluate_membership(db, request)

        assert result.memberships[missing_id] is False

    def test_rules_not_valid_are_false_and_the_others_still_answer(self):
        valid = _make_mock_segment(rules=US_RULES)
        legacy = _make_mock_segment(rules={"operator": "and", "conditions": []})
        db = MagicMock()
        by_id = {str(valid.id): valid, str(legacy.id): legacy}

        with patch.object(
            AudienceService, "get_segment", side_effect=lambda db, sid: by_id[sid]
        ):
            request = BulkSegmentMembershipRequest(
                user_context={"country": "US"},
                segment_ids=[str(legacy.id), str(valid.id)],
            )
            result = AudienceService.bulk_evaluate_membership(db, request)

        assert result.memberships == {str(legacy.id): False, str(valid.id): True}

    def test_bulk_evaluate_returns_bulk_response_instance(self):
        """bulk_evaluate_membership returns BulkSegmentMembershipResponse."""
        seg = _make_mock_segment(rules=US_RULES)
        db = MagicMock()

        with patch.object(AudienceService, "get_segment", return_value=seg):
            request = BulkSegmentMembershipRequest(
                user_context={},
                segment_ids=[str(seg.id)],
            )
            result = AudienceService.bulk_evaluate_membership(db, request)

        assert isinstance(result, BulkSegmentMembershipResponse)


# ---------------------------------------------------------------------------
# get_segment_experiments
# ---------------------------------------------------------------------------


class TestGetSegmentExperiments:
    def test_get_segment_experiments_raises_when_segment_not_found(self):
        """get_segment_experiments raises ValueError for non-existent segment."""
        db = _make_mock_db(segment=None)

        with pytest.raises(ValueError, match="not found"):
            AudienceService.get_segment_experiments(db, str(uuid.uuid4()))

    def test_get_segment_experiments_returns_response_with_correct_segment_id(self):
        """get_segment_experiments returns response with the queried segment_id."""
        mock_seg = _make_mock_segment()
        db = MagicMock()

        with patch.object(AudienceService, "get_segment", return_value=mock_seg):
            # Make DB query chains return empty lists gracefully
            db.query.return_value.filter.return_value.all.return_value = []

            result = AudienceService.get_segment_experiments(db, str(mock_seg.id))

        assert result.segment_id == str(mock_seg.id)
        assert isinstance(result.experiments, list)
        assert isinstance(result.feature_flags, list)


# ---------------------------------------------------------------------------
# preview_audience_size
# ---------------------------------------------------------------------------


def _exposure_rows(contexts, start=0):
    """``(user_id, event_metadata, created_at)`` rows, newest first, one user each."""
    return [
        (
            f"user-{start + i}",
            context,
            f"2026-11-06T12:{59 - (start + i) % 60:02d}:00+00:00",
        )
        for i, context in enumerate(contexts)
    ]


class _Rows(list):
    """The rows the read serves, and the row cap of each read (``caps``)."""

    def __init__(self):
        super().__init__()
        self.caps: list = []


@pytest.fixture
def exposures(monkeypatch):
    """Serve the preview's read from a list; returns the list to fill.

    The read is replaced, not the session, so these tests see only the
    sampling and the counting. The statement itself is covered against
    PostgreSQL in ``test_segment_preview_exposures.py``.
    """
    rows = _Rows()

    def read(db, since, until, cap):
        rows.caps.append(cap)
        yield from list(rows)

    monkeypatch.setattr(audience_service, "_read_recent_exposures", read)
    return rows


@pytest.fixture
def no_floor(monkeypatch):
    """A floor of 1 user, so a share of a handful is computed."""
    monkeypatch.setattr(audience_service, "PREVIEW_MIN_USERS", 1)


class TestPreviewAudienceSize:
    @pytest.mark.regression
    def test_a_time_window_that_cannot_be_read_counts_nobody(self, exposures):
        """A user is not counted, and the preview does not fail (#1145)."""
        exposures[:] = _exposure_rows(
            [{"country": "US", "now": "2026-01-05T12:00:00"}, {"country": "CA"}]
        )
        result = AudienceService.preview_audience_size(
            MagicMock(), UNREADABLE_WINDOW_RULES, sample_size=10
        )

        assert (result.sample_size, result.matched) == (2, 0)
        assert result.reason == "too_few_users"

    def test_preview_returns_the_nine_fields(self, exposures):
        """preview_audience_size returns an AudiencePreviewResponse, nine fields."""
        result = AudienceService.preview_audience_size(
            MagicMock(), US_RULES, sample_size=100
        )

        assert isinstance(result, AudiencePreviewResponse)
        assert set(result.model_dump()) == {
            "estimated_percentage",
            "sample_size",
            "matched",
            "source",
            "window_days",
            "sampled_since",
            "ended_by",
            "users_without_context",
            "reason",
        }

    @pytest.mark.regression
    def test_no_exposure_with_a_context_is_not_a_percentage(self, exposures):
        """On main an empty sample answered 0.0, read as "matches nobody"."""
        result = AudienceService.preview_audience_size(
            MagicMock(), US_RULES, sample_size=1000
        )

        assert result.model_dump() == {
            "estimated_percentage": None,
            "sample_size": 0,
            "matched": 0,
            "source": "exposure_metadata",
            "window_days": 30,
            "sampled_since": None,
            "ended_by": "window",
            "users_without_context": 0,
            "reason": "no_exposures_with_context",
        }

    def test_only_rows_with_a_context_object_count(self, exposures, no_floor):
        contexts = [{"country": "US"}, {"user": {"country": "US"}}, {"country": "DE"}]
        contexts += [{}, None, [], "US"]
        exposures[:] = _exposure_rows(contexts)
        result = AudienceService.preview_audience_size(
            MagicMock(), US_RULES, sample_size=500
        )

        assert result.estimated_percentage == 66.67
        assert (result.sample_size, result.matched) == (3, 2)
        assert result.users_without_context == 4
        assert result.sampled_since == exposures[-1][2]

    def test_a_users_latest_attributes_win(self, exposures, no_floor):
        pro = _rules(_eq("plan", "pro"))
        exposures[:] = [
            ("mover", {"plan": "pro"}, "2026-11-06T12:00:02+00:00"),
            ("other", {"plan": "basic"}, "2026-11-06T12:00:01+00:00"),
            ("mover", {"plan": "free"}, "2026-11-06T12:00:00+00:00"),
        ]
        result = AudienceService.preview_audience_size(MagicMock(), pro, 10)

        assert (result.matched, result.sample_size) == (1, 2)

    def test_a_user_whose_latest_exposure_has_no_context_is_not_sampled(
        self, exposures, no_floor
    ):
        exposures[:] = [
            ("u", {}, "2026-11-06T12:00:01+00:00"),
            ("u", {"country": "US"}, "2026-11-06T12:00:00+00:00"),
        ]
        result = AudienceService.preview_audience_size(MagicMock(), US_RULES, 10)

        assert (result.sample_size, result.users_without_context) == (0, 1)

    @pytest.mark.parametrize("users, percentage", [(29, None), (30, 100.0)])
    def test_the_floor_is_30_users(self, exposures, users, percentage):
        exposures[:] = _exposure_rows([{"country": "US"}] * users)
        result = AudienceService.preview_audience_size(MagicMock(), US_RULES, 1000)

        assert result.estimated_percentage == percentage
        assert result.reason == (None if percentage else "too_few_users")
        assert (result.matched, result.sample_size) == (users, users)

    def test_ended_by_says_what_ended_the_read(self, exposures):
        # 10 users asked for: the read takes at most 50 rows.
        exposures[:] = _exposure_rows([{"country": "US"}] * 12)
        target = AudienceService.preview_audience_size(MagicMock(), US_RULES, 10)
        assert (target.ended_by, target.sample_size) == ("sample_size", 10)
        assert target.sampled_since == exposures[9][2]
        assert exposures.caps == [50]

        exposures[:] = [("same", {"country": "US"}, f"t{i:02d}") for i in range(50)]
        capped = AudienceService.preview_audience_size(MagicMock(), US_RULES, 10)
        assert (capped.ended_by, capped.sample_size) == ("cap", 1)
        assert capped.sampled_since == "t49"

        exposures[:] = _exposure_rows([{"country": "US"}] * 5)
        window = AudienceService.preview_audience_size(MagicMock(), US_RULES, 10)
        assert (window.ended_by, window.sample_size) == ("window", 5)


class _Clock:
    """``time.monotonic`` for the read, moved only by the fake fetches."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def _no_context_rows(count: int, start: int = 0) -> list:
    """Rows of distinct users without attributes: the read never stops early."""
    return [(f"u{start + i}", None, f"t{start + i:03d}") for i in range(count)]


class TestPreviewRead:
    """The read's guards, without a database.

    Each fake fetch serves the next batch and moves the clock on by
    ``seconds_per_fetch``, so the budget each fetch is given is exact.
    """

    @pytest.fixture
    def clock(self, monkeypatch) -> _Clock:
        clock = _Clock()
        monkeypatch.setattr(audience_service, "time", clock)
        monkeypatch.setattr(audience_service, "PREVIEW_ROWS_PER_FETCH", 2)
        return clock

    @staticmethod
    def _db(
        dialect: str, clock: _Clock, batches, seconds_per_fetch: float
    ) -> MagicMock:
        db = MagicMock()
        db.get_bind.return_value.dialect.name = dialect
        pending = [list(batch) for batch in batches]

        def fetchmany(size):
            assert size == 2
            clock.now += seconds_per_fetch
            return pending.pop(0) if pending else []

        db.execute.return_value.fetchmany.side_effect = fetchmany
        return db

    @staticmethod
    def _executed(db) -> list:
        return [str(c.args[0]) for c in db.execute.call_args_list]

    @staticmethod
    def _budgets(db) -> list:
        """The ``statement_timeout`` set before each statement, in order."""
        return [
            c.args[1]["ms"]
            for c in db.execute.call_args_list
            if "set_config('statement_timeout', :ms, true)" in str(c.args[0])
        ]

    def test_postgresql_gives_each_fetch_what_is_left_of_one_budget(self, clock):
        batches = [_no_context_rows(2), _no_context_rows(2, 2), _no_context_rows(1, 4)]
        db = self._db("postgresql", clock, batches, seconds_per_fetch=1.0)
        result = AudienceService.preview_audience_size(db, US_RULES, 10)

        # The statement, then one budget per fetch: 5 s in all, not 5 s each.
        assert self._budgets(db) == ["5000", "5000", "4000", "3000"]
        executed = self._executed(db)
        assert len(executed) == 5
        assert "events.created_at DESC" in executed[1]
        assert db.execute.call_args_list[1].kwargs == {
            "execution_options": {"yield_per": 2}
        }
        # The short third batch ended the read: no fourth fetch.
        assert (result.users_without_context, result.ended_by) == (5, "window")

    @pytest.mark.parametrize(
        "seconds_per_fetch, budgets",
        [
            (2.0, ["5000", "5000", "3000", "1000"]),
            # Under a millisecond left: the read stops; 0 would turn the limit off.
            (4.9996, ["5000", "5000"]),
        ],
    )
    def test_the_read_stops_when_its_time_is_up(
        self, clock, seconds_per_fetch, budgets
    ):
        batches = [_no_context_rows(2, 2 * i) for i in range(10)]
        db = self._db("postgresql", clock, batches, seconds_per_fetch)
        with pytest.raises(audience_service.PreviewTimedOut):
            AudienceService.preview_audience_size(db, US_RULES, 10)

        assert self._budgets(db) == budgets
        assert "0" not in self._budgets(db)

    def test_another_database_skips_the_statement_timeout_but_keeps_the_deadline(
        self, clock
    ):
        batches = [_no_context_rows(2), _no_context_rows(1, 2)]
        db = self._db("sqlite", clock, batches, seconds_per_fetch=1.0)
        result = AudienceService.preview_audience_size(db, US_RULES, 10)
        executed = self._executed(db)
        assert len(executed) == 1
        assert "set_config" not in executed[0]
        assert result.users_without_context == 3

        db = self._db("sqlite", clock, batches, seconds_per_fetch=6.0)
        with pytest.raises(audience_service.PreviewTimedOut):
            AudienceService.preview_audience_size(db, US_RULES, 10)

    def test_only_sqlstate_57014_is_a_timeout(self):
        class DriverError(Exception):
            def __init__(self, pgcode):
                self.pgcode = pgcode

        class Wrapped(Exception):
            def __init__(self, orig):
                self.orig = orig

        assert audience_service._is_statement_timeout(DriverError("57014"))
        assert audience_service._is_statement_timeout(Wrapped(DriverError("57014")))
        assert not audience_service._is_statement_timeout(DriverError("22012"))
        assert not audience_service._is_statement_timeout(Wrapped(ValueError()))

    def test_a_timeout_raises_preview_timed_out_and_others_propagate(self, monkeypatch):
        class Canceled(Exception):
            pgcode = "57014"

        class Other(Exception):
            pgcode = "22012"

        for error, expected in (
            (Canceled, audience_service.PreviewTimedOut),
            (Other, Other),
        ):

            def read(db, since, until, cap, error=error):
                raise error()
                yield  # pragma: no cover

            monkeypatch.setattr(audience_service, "_read_recent_exposures", read)
            with pytest.raises(expected):
                AudienceService.preview_audience_size(MagicMock(), US_RULES, 10)
