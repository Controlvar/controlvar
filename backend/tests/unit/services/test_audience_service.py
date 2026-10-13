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
        db.query.return_value.offset.return_value.limit.return_value.all.return_value = [
            mock_seg
        ]

        results = AudienceService.list_segments(db)
        assert len(results) == 1

    def test_list_segments_filters_by_status(self):
        """list_segments with status filter applies filter to query."""
        db = MagicMock()
        db.query.return_value.filter.return_value.offset.return_value.limit.return_value.all.return_value = []

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


class TestPreviewAudienceSize:
    @staticmethod
    def _db(contexts):
        db = MagicMock()
        chain = db.query.return_value.filter.return_value.limit.return_value
        chain.all.return_value = [(c,) for c in contexts]
        return db

    @pytest.mark.regression
    def test_a_time_window_that_cannot_be_read_counts_nobody(self):
        """A user is not counted, and the preview does not fail (#1145)."""
        contexts = [{"country": "US", "now": "2026-01-05T12:00:00"}, {"country": "CA"}]
        result = AudienceService.preview_audience_size(
            self._db(contexts), UNREADABLE_WINDOW_RULES, sample_size=10
        )

        assert result.model_dump() == {
            "estimated_percentage": 0.0,
            "sample_size": 2,
            "matched": 0,
        }

    def test_preview_returns_dict_with_required_keys(self):
        """preview_audience_size returns AudiencePreviewResponse with required fields."""
        result = AudienceService.preview_audience_size(
            self._db([]), US_RULES, sample_size=100
        )

        assert isinstance(result, AudiencePreviewResponse)
        assert hasattr(result, "estimated_percentage")
        assert hasattr(result, "sample_size")
        assert hasattr(result, "matched")

    def test_preview_returns_zero_of_zero_when_no_assignment_has_a_context(self):
        """No context to evaluate: 0.0 of a sample of 0, never a share of rows."""
        result = AudienceService.preview_audience_size(
            self._db([]), US_RULES, sample_size=1000
        )

        assert result.model_dump() == {
            "estimated_percentage": 0.0,
            "sample_size": 0,
            "matched": 0,
        }

    def test_only_rows_with_a_context_object_count(self):
        contexts = [{"country": "US"}, {"user": {"country": "US"}}, {"country": "DE"}]
        contexts += [{}, None, [], "US"]
        result = AudienceService.preview_audience_size(
            self._db(contexts), US_RULES, sample_size=500
        )

        assert result.model_dump() == {
            "estimated_percentage": 66.67,
            "sample_size": 3,
            "matched": 2,
        }
