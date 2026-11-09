# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import re
import uuid
from datetime import timedelta

from django.utils import timezone

# The date from pattern
pattern = re.compile(r"\d+_(weeks|months)$")


# check the valid uuids
def filter_valid_uuids(uuid_list):
    valid_uuids = []
    for uuid_str in uuid_list:
        try:
            uuid_obj = uuid.UUID(uuid_str)
            valid_uuids.append(uuid_obj)
        except ValueError:
            # ignore the invalid uuids
            pass
    return valid_uuids


def _filter_membership(
    params,
    issue_filter,
    method,
    prefix,
    *,
    field,
    lookup,
    parse_uuids=False,
    null_lookup=None,
):
    value = params.get(field)
    if method != "GET":
        if value and len(value) and value != "null":
            issue_filter[f"{prefix}{lookup}"] = value
        return issue_filter

    values = [item for item in value.split(",") if item != "null"]
    if null_lookup is not None and "None" in values:
        issue_filter[f"{prefix}{null_lookup}"] = True
    if parse_uuids:
        values = filter_valid_uuids(values)
    if values and "" not in values:
        issue_filter[f"{prefix}{lookup}"] = values
    return issue_filter


# Get the 2_weeks, 3_months
def string_date_filter(issue_filter, duration, subsequent, term, date_filter, offset):
    now = timezone.now().date()
    if term == "months":
        if subsequent == "after":
            if offset == "fromnow":
                issue_filter[f"{date_filter}__gte"] = now + timedelta(days=duration * 30)
            else:
                issue_filter[f"{date_filter}__gte"] = now - timedelta(days=duration * 30)
        else:
            if offset == "fromnow":
                issue_filter[f"{date_filter}__lte"] = now + timedelta(days=duration * 30)
            else:
                issue_filter[f"{date_filter}__lte"] = now - timedelta(days=duration * 30)
    if term == "weeks":
        if subsequent == "after":
            if offset == "fromnow":
                issue_filter[f"{date_filter}__gte"] = now + timedelta(weeks=duration)
            else:
                issue_filter[f"{date_filter}__gte"] = now - timedelta(weeks=duration)
        else:
            if offset == "fromnow":
                issue_filter[f"{date_filter}__lte"] = now + timedelta(weeks=duration)
            else:
                issue_filter[f"{date_filter}__lte"] = now - timedelta(weeks=duration)


def date_filter(issue_filter, date_term, queries):
    """
    Handle all date filters
    """
    for query in queries:
        date_query = query.split(";")
        if date_query:
            if len(date_query) >= 2:
                match = pattern.match(date_query[0])
                if match:
                    if len(date_query) == 3:
                        digit, term = date_query[0].split("_")
                        string_date_filter(
                            issue_filter=issue_filter,
                            duration=int(digit),
                            subsequent=date_query[1],
                            term=term,
                            date_filter=date_term,
                            offset=date_query[2],
                        )
                else:
                    if "after" in date_query:
                        issue_filter[f"{date_term}__gte"] = date_query[0]
                    else:
                        issue_filter[f"{date_term}__lte"] = date_query[0]
            else:
                issue_filter[f"{date_term}__contains"] = date_query[0]


def filter_state(params, issue_filter, method, prefix=""):
    return _filter_membership(params, issue_filter, method, prefix, field="state", lookup="state__in", parse_uuids=True)


def filter_state_group(params, issue_filter, method, prefix=""):
    return _filter_membership(params, issue_filter, method, prefix, field="state_group", lookup="state__group__in")


def filter_estimate_point(params, issue_filter, method, prefix=""):
    return _filter_membership(params, issue_filter, method, prefix, field="estimate_point", lookup="estimate_point__in")


def filter_priority(params, issue_filter, method, prefix=""):
    return _filter_membership(params, issue_filter, method, prefix, field="priority", lookup="priority__in")


def filter_parent(params, issue_filter, method, prefix=""):
    return _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="parent",
        lookup="parent__in",
        parse_uuids=True,
        null_lookup="parent__isnull",
    )


def filter_labels(params, issue_filter, method, prefix=""):
    _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="labels",
        lookup="labels__in",
        parse_uuids=True,
        null_lookup="labels__isnull",
    )
    issue_filter[f"{prefix}label_issue__deleted_at__isnull"] = True
    return issue_filter


def filter_assignees(params, issue_filter, method, prefix=""):
    _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="assignees",
        lookup="assignees__in",
        parse_uuids=True,
        null_lookup="assignees__isnull",
    )
    issue_filter[f"{prefix}issue_assignee__deleted_at__isnull"] = True
    return issue_filter


def filter_mentions(params, issue_filter, method, prefix=""):
    return _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="mentions",
        lookup="issue_mention__mention__id__in",
        parse_uuids=True,
    )


def filter_created_by(params, issue_filter, method, prefix=""):
    return _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="created_by",
        lookup="created_by__in",
        parse_uuids=True,
        null_lookup="created_by__isnull",
    )


def filter_name(params, issue_filter, method, prefix=""):
    if params.get("name", "") != "":
        issue_filter[f"{prefix}name__icontains"] = params.get("name")
    return issue_filter


def filter_created_at(params, issue_filter, method, prefix=""):
    if method == "GET":
        created_ats = params.get("created_at").split(",")
        if len(created_ats) and "" not in created_ats:
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}created_at__date",
                queries=created_ats,
            )
    else:
        if params.get("created_at", None) and len(params.get("created_at")):
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}created_at__date",
                queries=params.get("created_at", []),
            )
    return issue_filter


def filter_updated_at(params, issue_filter, method, prefix=""):
    if method == "GET":
        updated_ats = params.get("updated_at").split(",")
        if len(updated_ats) and "" not in updated_ats:
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}updated_at__date",
                queries=updated_ats,
            )
    else:
        if params.get("updated_at", None) and len(params.get("updated_at")):
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}updated_at__date",
                queries=params.get("updated_at", []),
            )
    return issue_filter


def filter_start_date(params, issue_filter, method, prefix=""):
    if method == "GET":
        start_dates = params.get("start_date").split(",")
        if len(start_dates) and "" not in start_dates:
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}start_date",
                queries=start_dates,
            )
    else:
        if params.get("start_date", None) and len(params.get("start_date")):
            issue_filter[f"{prefix}start_date"] = params.get("start_date")
    return issue_filter


def filter_target_date(params, issue_filter, method, prefix=""):
    if method == "GET":
        target_dates = params.get("target_date").split(",")
        if len(target_dates) and "" not in target_dates:
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}target_date",
                queries=target_dates,
            )
    else:
        if params.get("target_date", None) and len(params.get("target_date")):
            issue_filter[f"{prefix}target_date"] = params.get("target_date")
    return issue_filter


def filter_completed_at(params, issue_filter, method, prefix=""):
    if method == "GET":
        completed_ats = params.get("completed_at").split(",")
        if len(completed_ats) and "" not in completed_ats:
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}completed_at__date",
                queries=completed_ats,
            )
    else:
        if params.get("completed_at", None) and len(params.get("completed_at")):
            date_filter(
                issue_filter=issue_filter,
                date_term=f"{prefix}completed_at__date",
                queries=params.get("completed_at", []),
            )
    return issue_filter


def filter_issue_state_type(params, issue_filter, method, prefix=""):
    type = params.get("type", "all")
    group = ["backlog", "unstarted", "started", "completed", "cancelled"]
    if type == "backlog":
        group = ["backlog"]
    if type == "active":
        group = ["unstarted", "started"]

    issue_filter[f"{prefix}state__group__in"] = group
    return issue_filter


def filter_project(params, issue_filter, method, prefix=""):
    return _filter_membership(
        params, issue_filter, method, prefix, field="project", lookup="project__in", parse_uuids=True
    )


def filter_cycle(params, issue_filter, method, prefix=""):
    _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="cycle",
        lookup="issue_cycle__cycle_id__in",
        parse_uuids=True,
        null_lookup="issue_cycle__cycle_id__isnull",
    )
    issue_filter[f"{prefix}issue_cycle__deleted_at__isnull"] = True
    return issue_filter


def filter_module(params, issue_filter, method, prefix=""):
    _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="module",
        lookup="issue_module__module_id__in",
        parse_uuids=True,
        null_lookup="issue_module__module_id__isnull",
    )
    issue_filter[f"{prefix}issue_module__deleted_at__isnull"] = True
    return issue_filter


def filter_intake_status(params, issue_filter, method, prefix=""):
    if method == "GET":
        return _filter_membership(
            params, issue_filter, method, prefix, field="intake_status", lookup="issue_intake__status__in"
        )

    # Preserve the legacy body behavior, which reads the value from inbox_status.
    value = params.get("intake_status")
    if value and len(value) and value != "null":
        issue_filter[f"{prefix}issue_intake__status__in"] = params.get("inbox_status")
    return issue_filter


def filter_inbox_status(params, issue_filter, method, prefix=""):
    return _filter_membership(
        params, issue_filter, method, prefix, field="inbox_status", lookup="issue_intake__status__in"
    )


def filter_sub_issue_toggle(params, issue_filter, method, prefix=""):
    if method == "GET":
        sub_issue = params.get("sub_issue", "false")
        if sub_issue == "false":
            issue_filter[f"{prefix}parent__isnull"] = True
    else:
        sub_issue = params.get("sub_issue", "false")
        if sub_issue == "false":
            issue_filter[f"{prefix}parent__isnull"] = True
    return issue_filter


def filter_subscribed_issues(params, issue_filter, method, prefix=""):
    _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="subscriber",
        lookup="issue_subscribers__subscriber_id__in",
        parse_uuids=True,
    )
    issue_filter[f"{prefix}issue_subscribers__deleted_at__isnull"] = True
    return issue_filter


def filter_start_target_date_issues(params, issue_filter, method, prefix=""):
    start_target_date = params.get("start_target_date", "false")
    if start_target_date == "true":
        issue_filter[f"{prefix}target_date__isnull"] = False
        issue_filter[f"{prefix}start_date__isnull"] = False
    return issue_filter


def filter_logged_by(params, issue_filter, method, prefix=""):
    return _filter_membership(
        params,
        issue_filter,
        method,
        prefix,
        field="logged_by",
        lookup="logged_by__in",
        parse_uuids=True,
        null_lookup="logged_by__isnull",
    )


def issue_filters(query_params, method, prefix=""):
    issue_filter = {}

    ISSUE_FILTER = {
        "state": filter_state,
        "state_group": filter_state_group,
        "estimate_point": filter_estimate_point,
        "priority": filter_priority,
        "parent": filter_parent,
        "labels": filter_labels,
        "assignees": filter_assignees,
        "mentions": filter_mentions,
        "created_by": filter_created_by,
        "logged_by": filter_logged_by,
        "name": filter_name,
        "created_at": filter_created_at,
        "updated_at": filter_updated_at,
        "start_date": filter_start_date,
        "target_date": filter_target_date,
        "completed_at": filter_completed_at,
        "type": filter_issue_state_type,
        "project": filter_project,
        "cycle": filter_cycle,
        "module": filter_module,
        "intake_status": filter_intake_status,
        "inbox_status": filter_inbox_status,
        "sub_issue": filter_sub_issue_toggle,
        "subscriber": filter_subscribed_issues,
        "start_target_date": filter_start_target_date_issues,
    }

    for key, value in ISSUE_FILTER.items():
        if key in query_params:
            func = value
            func(query_params, issue_filter, method, prefix)
    return issue_filter
