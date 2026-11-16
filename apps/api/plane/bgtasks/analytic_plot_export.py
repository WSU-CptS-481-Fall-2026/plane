# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import csv
import io
import logging

# Third party imports
from celery import shared_task

# Django imports
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.db.models import Q, Case, Value, When
from django.db import models
from django.db.models.functions import Cast, Concat

# Module imports
from plane.db.models import Issue
from plane.license.utils.instance_value import get_email_configuration
from plane.utils.analytics_plot import build_graph_plot
from plane.utils.email import generate_plain_text_from_html
from plane.utils.exception_logger import log_exception
from plane.utils.issue_filters import issue_filters
from plane.utils.csv_utils import sanitize_csv_row

row_mapping = {
    "state__name": "State",
    "state__group": "State Group",
    "labels__id": "Label",
    "assignees__id": "Assignee Name",
    "start_date": "Start Date",
    "target_date": "Due Date",
    "completed_at": "Completed At",
    "created_at": "Created At",
    "issue_count": "Issue Count",
    "priority": "Priority",
    "estimate": "Estimate",
    "issue_cycle__cycle_id": "Cycle",
    "issue_module__module_id": "Module",
}

ASSIGNEE_ID = "assignees__id"
LABEL_ID = "labels__id"
STATE_ID = "state_id"
CYCLE_ID = "issue_cycle__cycle_id"
MODULE_ID = "issue_module__module_id"


def send_export_email(email, slug, csv_buffer, rows):
    """Helper function to send export email."""
    subject = "Your Export is ready"
    html_content = render_to_string("emails/exports/analytics.html", {})
    text_content = generate_plain_text_from_html(html_content)

    csv_buffer.seek(0)

    (
        EMAIL_HOST,
        EMAIL_HOST_USER,
        EMAIL_HOST_PASSWORD,
        EMAIL_PORT,
        EMAIL_USE_TLS,
        EMAIL_USE_SSL,
        EMAIL_FROM,
    ) = get_email_configuration()

    connection = get_connection(
        host=EMAIL_HOST,
        port=int(EMAIL_PORT),
        username=EMAIL_HOST_USER,
        password=EMAIL_HOST_PASSWORD,
        use_tls=EMAIL_USE_TLS == "1",
        use_ssl=EMAIL_USE_SSL == "1",
    )

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_content,
        from_email=EMAIL_FROM,
        to=[email],
        connection=connection,
    )
    msg.attach(f"{slug}-analytics.csv", csv_buffer.getvalue())
    msg.send(fail_silently=False)
    return


def get_assignee_details(slug, filters):
    """Fetch assignee details if required."""
    return (
        Issue.issue_objects.filter(
            Q(Q(assignees__avatar__isnull=False) | Q(assignees__avatar_asset__isnull=False)),
            workspace__slug=slug,
            **filters,
        )
        .annotate(
            assignees__avatar_url=Case(
                # If `avatar_asset` exists, use it to generate the asset URL
                When(
                    assignees__avatar_asset__isnull=False,
                    then=Concat(
                        Value("/api/assets/v2/static/"),
                        Cast("assignees__avatar_asset", models.CharField()),
                        Value("/"),
                    ),
                ),
                # If `avatar_asset` is None, fall back to using `avatar` field directly
                When(assignees__avatar_asset__isnull=True, then="assignees__avatar"),
                default=Value(None),
                output_field=models.CharField(),
            )
        )
        .distinct("assignees__id")
        .order_by("assignees__id")
        .values(
            "assignees__avatar_url",
            "assignees__display_name",
            "assignees__first_name",
            "assignees__last_name",
            "assignees__id",
        )
    )


def get_label_details(slug, filters):
    """Fetch label details if required"""
    return (
        Issue.objects.filter(
            workspace__slug=slug,
            **filters,
            labels__id__isnull=False,
            label_issue__deleted_at__isnull=True,
        )
        .distinct("labels__id")
        .order_by("labels__id")
        .values("labels__id", "labels__color", "labels__name")
    )


def get_state_details(slug, filters):
    return (
        Issue.issue_objects.filter(workspace__slug=slug, **filters)
        .distinct("state_id")
        .order_by("state_id")
        .values("state_id", "state__name", "state__color")
    )


def get_module_details(slug, filters):
    return (
        Issue.issue_objects.filter(
            workspace__slug=slug,
            **filters,
            issue_module__module_id__isnull=False,
            issue_module__deleted_at__isnull=True,
        )
        .distinct("issue_module__module_id")
        .order_by("issue_module__module_id")
        .values("issue_module__module_id", "issue_module__module__name")
    )


def get_cycle_details(slug, filters):
    return (
        Issue.issue_objects.filter(
            workspace__slug=slug,
            **filters,
            issue_cycle__cycle_id__isnull=False,
            issue_cycle__deleted_at__isnull=True,
        )
        .distinct("issue_cycle__cycle_id")
        .order_by("issue_cycle__cycle_id")
        .values("issue_cycle__cycle_id", "issue_cycle__cycle__name")
    )


def generate_csv_from_rows(rows):
    """Generate CSV buffer from rows."""
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer, delimiter=",", quoting=csv.QUOTE_ALL)
    [writer.writerow(sanitize_csv_row(row)) for row in rows]
    return csv_buffer

# Get the unique segment values.
def get_segment_values(distribution):
    return list(
        set(
            item.get("segment")
            for sublist in distribution.values()
            for item in sublist
        )
    )


# Make the data rows with totals and segment values.
def generate_segment_data_rows(distribution, segment_values, key):
    rows = []

    for item, data in distribution.items():
        generated_row = [
            item,
            sum(obj.get(key) for obj in data if obj.get(key) is not None),
        ]

        for segment_value in segment_values:
            value = next(
                (
                    entry.get(key)
                    for entry in data
                    if entry.get("segment") == segment_value
                ),
                "0",
            )
            generated_row.append(value)

        rows.append(tuple(generated_row))

    return rows


# Get the display name for the selected ID.
def resolve_display_name(item, dimension, details):
    dimension_fields = {
        LABEL_ID: (LABEL_ID, "labels__name"),
        STATE_ID: (STATE_ID, "state__name"),
        CYCLE_ID: (CYCLE_ID, "issue_cycle__cycle__name"),
        MODULE_ID: (MODULE_ID, "issue_module__module__name"),
    }

    if dimension == ASSIGNEE_ID:
        detail = next(
            (user for user in details if str(user[ASSIGNEE_ID]) == str(item)),
            None,
        )
        if detail:
            return f"{detail['assignees__first_name']} {detail['assignees__last_name']}"

    elif dimension in dimension_fields:
        id_field, name_field = dimension_fields[dimension]
        detail = next(
            (entry for entry in details if str(entry[id_field]) == str(item)),
            None,
        )
        if detail:
            return detail[name_field]

    return item

# Make the CSV rows for segmented analytics.
def generate_segmented_rows(
    distribution,
    x_axis,
    y_axis,
    segment,
    key,
    details_by_dimension,
):
    segment_values = get_segment_values(distribution)

    row_zero = [
        row_mapping.get(x_axis, "X-Axis"),
        row_mapping.get(y_axis, "Y-Axis"),
    ] + segment_values

    rows = generate_segment_data_rows(distribution, segment_values, key)

    x_axis_details = details_by_dimension.get(x_axis, [])
    rows = [
        (
            resolve_display_name(row[0], x_axis, x_axis_details),
            *row[1:],
        )
        for row in rows
    ]

    for index, segment_value in enumerate(row_zero[2:]):
        if segment == MODULE_ID:
            detail = next(
                (mod for mod in details_by_dimension[MODULE_ID] if str(mod[MODULE_ID]) == str(segment_value)),
                None,
            )
            if detail:
                row_zero[index + 2] = detail["issue_module__module__name"]
        else:
            row_zero[index + 2] = resolve_display_name(
                segment_value, segment, details_by_dimension.get(segment, [])
            )

    return [tuple(row_zero)] + rows

# Make the CSV rows for non-segmented analytics.
def generate_non_segmented_rows(
    distribution,
    x_axis,
    y_axis,
    key,
    details_by_dimension,
):
    rows = []

    for item, data in distribution.items():
        display_name = resolve_display_name(
            item, x_axis, details_by_dimension.get(x_axis, [])
        )
        rows.append((display_name, data[0].get(key)))

    header = (
        row_mapping.get(x_axis, "X-Axis"),
        row_mapping.get(y_axis, "Y-Axis"),
    )

    return [header] + rows

# Get the details for the selected analytics dimensions.
def get_analytics_dimension_details(slug, filters, x_axis, segment):
    detail_functions = {
        ASSIGNEE_ID: get_assignee_details,
        LABEL_ID: get_label_details,
        STATE_ID: get_state_details,
        CYCLE_ID: get_cycle_details,
        MODULE_ID: get_module_details,
    }

    details = {}

    for dimension, get_details in detail_functions.items():
        if x_axis == dimension or segment == dimension:
            details[dimension] = get_details(slug, filters)
        else:
            details[dimension] = {}

    return details

@shared_task
def analytic_export_task(email, data, slug):
    try:
        filters = issue_filters(data, "POST")
        queryset = Issue.issue_objects.filter(**filters, workspace__slug=slug)

        x_axis = data.get("x_axis", False)
        y_axis = data.get("y_axis", False)
        segment = data.get("segment", False)

        distribution = build_graph_plot(queryset, x_axis=x_axis, y_axis=y_axis, segment=segment)
        key = "count" if y_axis == "issue_count" else "estimate"

        details_by_dimension = get_analytics_dimension_details(
            slug, filters, x_axis, segment
        )

        if segment:
            rows = generate_segmented_rows(
                distribution,
                x_axis,
                y_axis,
                segment,
                key,
                details_by_dimension,
            )
        else:
            rows = generate_non_segmented_rows(
                distribution,
                x_axis,
                y_axis,
                key,
                details_by_dimension,
            )
            
        csv_buffer = generate_csv_from_rows(rows)
        send_export_email(email, slug, csv_buffer, rows)
        logging.getLogger("plane.worker").info("Email sent successfully.")
        return
    except Exception as e:
        log_exception(e)
        return


@shared_task
def export_analytics_to_csv_email(data, headers, keys, email, slug):
    try:
        """
        Prepares a CSV from data and sends it as an email attachment.

        Parameters:
        - data: List of dictionaries (e.g. from .values())
        - headers: List of CSV column headers
        - keys: Keys to extract from each data item (dict)
        - email: Email address to send to
        - slug: Used for the filename
        """
        # Prepare rows: header + data rows
        rows = [headers]
        for item in data:
            row = [item.get(key, "") for key in keys]
            rows.append(row)

        # Generate CSV buffer
        csv_buffer = generate_csv_from_rows(rows)

        # Send email with CSV attachment
        send_export_email(email=email, slug=slug, csv_buffer=csv_buffer, rows=rows)
    except Exception as e:
        log_exception(e)
        return
