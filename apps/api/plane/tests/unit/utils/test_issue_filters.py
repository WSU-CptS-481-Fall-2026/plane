# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from django.http import QueryDict

from plane.utils.issue_filters import filter_updated_at, issue_filters


FILTER_ID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.mark.unit
class TestFilterUpdatedAt:
    """Regression test for the "Updated At" filter bug — the filter was
    applying to the `created_at` column instead of `updated_at`, so work
    items updated today but created earlier never showed up under
    `Updated At -> is -> today`. See issue #9316."""

    def test_get_method_targets_updated_at_column(self):
        issue_filter = {}
        params = {"updated_at": "2026-06-26"}
        filter_updated_at(params, issue_filter, method="GET")
        assert issue_filter == {"updated_at__date__contains": "2026-06-26"}

    def test_get_method_with_csv_targets_updated_at_column(self):
        issue_filter = {}
        params = {"updated_at": "2026-06-25,2026-06-26"}
        filter_updated_at(params, issue_filter, method="GET")
        assert issue_filter == {"updated_at__date__contains": "2026-06-26"}

    def test_post_method_targets_updated_at_column(self):
        issue_filter = {}
        params = {"updated_at": ["2026-06-26"]}
        filter_updated_at(params, issue_filter, method="POST")
        assert issue_filter == {"updated_at__date__contains": "2026-06-26"}

    def test_get_method_with_prefix_targets_updated_at_column(self):
        issue_filter = {}
        params = {"updated_at": "2026-06-26"}
        filter_updated_at(params, issue_filter, method="GET", prefix="cycle_issue__")
        assert issue_filter == {"cycle_issue__updated_at__date__contains": "2026-06-26"}

    def test_empty_get_filter_is_noop(self):
        issue_filter = {}
        params = {"updated_at": ""}
        filter_updated_at(params, issue_filter, method="GET")
        assert issue_filter == {}


@pytest.mark.unit
class TestIssueFilters:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("unknown=value", {}, id="unknown-field"),
            pytest.param("priority=high,null,low", {"priority__in": ["high", "low"]}, id="plain-list"),
            pytest.param("priority=high,,low", {}, id="plain-list-with-empty-token"),
            pytest.param("priority=null", {}, id="plain-list-with-only-null"),
            pytest.param("state_group=", {}, id="empty-state-group"),
            pytest.param("estimate_point=1,2", {"estimate_point__in": ["1", "2"]}, id="estimate-points"),
            pytest.param("estimate_point=", {}, id="empty-estimate-points"),
            pytest.param("intake_status=", {}, id="empty-intake-status"),
            pytest.param("inbox_status=", {}, id="empty-inbox-status"),
            pytest.param("name=example", {"name__icontains": "example"}, id="name-contains"),
            pytest.param("sub_issue=false", {"parent__isnull": True}, id="exclude-sub-issues"),
            pytest.param("sub_issue=true", {}, id="include-sub-issues"),
            pytest.param(
                "start_target_date=true",
                {"start_date__isnull": False, "target_date__isnull": False},
                id="require-both-dates",
            ),
        ],
    )
    def test_get_query_params(self, query, expected):
        assert issue_filters(QueryDict(query), "GET") == expected

    @pytest.mark.parametrize(
        ("field", "lookup", "null_lookups", "relation_lookups"),
        [
            pytest.param("state", "state__in", {}, {}, id="state"),
            pytest.param("project", "project__in", {}, {}, id="project"),
            pytest.param("mentions", "issue_mention__mention__id__in", {}, {}, id="mentions"),
            pytest.param("parent", "parent__in", {"parent__isnull": True}, {}, id="parent"),
            pytest.param("created_by", "created_by__in", {"created_by__isnull": True}, {}, id="created-by"),
            pytest.param("logged_by", "logged_by__in", {"logged_by__isnull": True}, {}, id="logged-by"),
            pytest.param(
                "labels",
                "labels__in",
                {"labels__isnull": True},
                {"label_issue__deleted_at__isnull": True},
                id="labels",
            ),
            pytest.param(
                "assignees",
                "assignees__in",
                {"assignees__isnull": True},
                {"issue_assignee__deleted_at__isnull": True},
                id="assignees",
            ),
            pytest.param(
                "cycle",
                "issue_cycle__cycle_id__in",
                {"issue_cycle__cycle_id__isnull": True},
                {"issue_cycle__deleted_at__isnull": True},
                id="cycle",
            ),
            pytest.param(
                "module",
                "issue_module__module_id__in",
                {"issue_module__module_id__isnull": True},
                {"issue_module__deleted_at__isnull": True},
                id="module",
            ),
            pytest.param(
                "subscriber",
                "issue_subscribers__subscriber_id__in",
                {},
                {"issue_subscribers__deleted_at__isnull": True},
                id="subscriber",
            ),
        ],
    )
    @pytest.mark.parametrize(
        ("method", "value", "expected_ids", "includes_null"),
        [
            pytest.param("GET", f"None,{FILTER_ID},,invalid,null", [FILTER_ID], True, id="get-mixed"),
            pytest.param("GET", str(FILTER_ID), [FILTER_ID], False, id="get-uuid"),
            pytest.param("GET", "invalid,,null", [], False, id="get-invalid"),
            pytest.param("POST", [str(FILTER_ID)], [str(FILTER_ID)], False, id="post-uuid"),
            pytest.param("POST", [], [], False, id="post-empty"),
            pytest.param("PATCH", [str(FILTER_ID)], [str(FILTER_ID)], False, id="patch-uuid"),
            pytest.param("PATCH", [], [], False, id="patch-empty"),
        ],
    )
    def test_uuid_filters(
        self, field, lookup, null_lookups, relation_lookups, method, value, expected_ids, includes_null
    ):
        expected = relation_lookups.copy()
        if expected_ids:
            expected[lookup] = expected_ids
        if includes_null:
            expected.update(null_lookups)
        assert issue_filters({field: value}, method) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(
                f"None,{FILTER_ID}",
                {
                    "issue__labels__isnull": True,
                    "issue__labels__in": [FILTER_ID],
                    "issue__label_issue__deleted_at__isnull": True,
                },
                id="null-and-uuid",
            ),
            pytest.param("", {"issue__label_issue__deleted_at__isnull": True}, id="empty"),
        ],
    )
    def test_get_labels_with_prefix(self, value, expected):
        assert issue_filters({"labels": value}, "GET", prefix="issue__") == expected

    @pytest.mark.parametrize("method", ["POST", "PATCH"])
    def test_body_filters_preserve_values(self, method):
        params = {"state": [str(FILTER_ID)], "priority": "null", "labels": [], "sub_issue": False}
        expected = {"state__in": [str(FILTER_ID)], "label_issue__deleted_at__isnull": True}
        assert issue_filters(params, method) == expected

    @pytest.mark.parametrize("method", ["POST", "PATCH"])
    @pytest.mark.parametrize(
        ("field", "lookup", "values"),
        [
            ("state_group", "state__group__in", ["completed"]),
            ("estimate_point", "estimate_point__in", [1, 2]),
            ("priority", "priority__in", ["high"]),
            ("inbox_status", "issue_intake__status__in", [1, 2]),
        ],
    )
    def test_body_membership_lists(self, method, field, lookup, values):
        assert issue_filters({field: values}, method) == {lookup: values}
        assert issue_filters({field: []}, method) == {}

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({"state_group": "completed", "type": "active"}, {"state__group__in": ["unstarted", "started"]}),
            ({"intake_status": "1", "inbox_status": "2"}, {"issue_intake__status__in": ["2"]}),
        ],
        ids=["type-over-state-group", "inbox-over-intake"],
    )
    def test_shared_lookup_precedence(self, params, expected):
        assert issue_filters(params, "GET") == expected
        assert issue_filters(dict(reversed(params.items())), "GET") == expected

    def test_intake_status_alone_in_body(self):
        # The legacy body handler reads inbox_status even when only intake_status is present.
        assert issue_filters({"intake_status": ["1"]}, "PATCH") == {"issue_intake__status__in": None}

    @pytest.mark.parametrize("method", ["POST", "PATCH"])
    def test_empty_intake_status_in_body(self, method):
        assert issue_filters({"intake_status": []}, method) == {}


@pytest.mark.unit
class TestIssueFilterDates:
    @pytest.mark.parametrize(
        ("field", "lookup"),
        [
            ("created_at", "created_at__date"),
            ("updated_at", "updated_at__date"),
            ("completed_at", "completed_at__date"),
            ("start_date", "start_date"),
            ("target_date", "target_date"),
        ],
    )
    def test_get_absolute_date_range(self, field, lookup):
        params = {field: "2026-06-01;after,2026-06-26;before"}
        assert issue_filters(params, "GET") == {
            f"{lookup}__gte": "2026-06-01",
            f"{lookup}__lte": "2026-06-26",
        }

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("2_weeks;after;fromnow", {"target_date__gte": date(2026, 7, 10)}),
            ("2_weeks;before;fromnow", {"target_date__lte": date(2026, 7, 10)}),
            ("2_weeks;after;ago", {"target_date__gte": date(2026, 6, 12)}),
            ("2_weeks;before;ago", {"target_date__lte": date(2026, 6, 12)}),
            ("1_months;after;fromnow", {"target_date__gte": date(2026, 7, 26)}),
            ("1_months;before;fromnow", {"target_date__lte": date(2026, 7, 26)}),
            ("1_months;after;ago", {"target_date__gte": date(2026, 5, 27)}),
            ("1_months;before;ago", {"target_date__lte": date(2026, 5, 27)}),
            ("2_weeks;after", {}),
        ],
    )
    def test_get_relative_date(self, monkeypatch, expression, expected):
        monkeypatch.setattr(
            "plane.utils.issue_filters.timezone.now",
            lambda: datetime(2026, 6, 26, tzinfo=timezone.utc),
        )
        assert issue_filters({"target_date": expression}, "GET") == expected

    @pytest.mark.parametrize("method", ["POST", "PATCH"])
    def test_body_dates_use_field_specific_lookups(self, method):
        params = {
            "created_at": ["2026-06-26"],
            "updated_at": ["2026-06-26"],
            "completed_at": ["2026-06-26"],
            "start_date": "2026-06-26",
            "target_date": "2026-06-26",
        }
        assert issue_filters(params, method) == {
            "created_at__date__contains": "2026-06-26",
            "updated_at__date__contains": "2026-06-26",
            "completed_at__date__contains": "2026-06-26",
            "start_date": "2026-06-26",
            "target_date": "2026-06-26",
        }

    @pytest.mark.parametrize(
        ("method", "timestamp_value", "date_value"),
        [
            pytest.param("GET", "", "", id="get-empty"),
            pytest.param("GET", "2026-06-26,", "2026-06-26,", id="get-trailing-comma"),
            pytest.param("POST", [], "", id="post-empty"),
            pytest.param("PATCH", [], "", id="patch-empty"),
        ],
    )
    def test_empty_date_filters(self, method, timestamp_value, date_value):
        params = {
            "created_at": timestamp_value,
            "updated_at": timestamp_value,
            "completed_at": timestamp_value,
            "start_date": date_value,
            "target_date": date_value,
        }
        assert issue_filters(params, method) == {}
