
# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import csv

# Third party imports
import pytest

# Module imports
from plane.bgtasks.analytic_plot_export import (
    ASSIGNEE_ID,
    CYCLE_ID,
    LABEL_ID,
    MODULE_ID,
    STATE_ID,
    generate_csv_from_rows,
    generate_non_segmented_rows,
    generate_segmented_rows,
)
import io
from unittest.mock import MagicMock

import plane.bgtasks.analytic_plot_export as analytic_export

# Tests for CSV row generation
@pytest.mark.parametrize(
    "x_axis,details,expected",
    [
        (
            ASSIGNEE_ID,
            [
                {
                    ASSIGNEE_ID: "1",
                    "assignees__first_name": "John",
                    "assignees__last_name": "Smith",
                }
            ],
            "John Smith",
        ),
        (
            LABEL_ID,
            [{LABEL_ID: "1", "labels__name": "Bug"}],
            "Bug",
        ),
        (
            STATE_ID,
            [{STATE_ID: "1", "state__name": "Completed"}],
            "Completed",
        ),
        (
            CYCLE_ID,
            [{CYCLE_ID: "1", "issue_cycle__cycle__name": "Sprint 1"}],
            "Sprint 1",
        ),
        (
            MODULE_ID,
            [{MODULE_ID: "1", "issue_module__module__name": "Backend"}],
            "Backend",
        ),
    ],
)
# Tests that IDs are converted into readable names in nonsegmented rows.
def test_NonSegmentedRowsResolveDisplayNames(x_axis, details, expected):
    details_by_dimension = {
        ASSIGNEE_ID: details if x_axis == ASSIGNEE_ID else [],
        LABEL_ID: details if x_axis == LABEL_ID else [],
        STATE_ID: details if x_axis == STATE_ID else [],
        CYCLE_ID: details if x_axis == CYCLE_ID else [],
        MODULE_ID: details if x_axis == MODULE_ID else [],
    }

    rows = generate_non_segmented_rows(
        distribution={"1": [{"count": 5, "estimate": 10}]},
        x_axis=x_axis,
        y_axis="issue_count",
        key="count",
        details_by_dimension=details_by_dimension,
    )

    assert rows[1][0] == expected
    assert rows[1][1] == 5


# Tests that nonsegmented rows include the correct column headers.
def test_NonSegmentedRowsReturnsCorrectHeaders():
    rows = generate_non_segmented_rows(
        distribution={"Open": [{"count": 3, "estimate": 8}]},
        x_axis="state__name",
        y_axis="issue_count",
        key="count",
        details_by_dimension={},
    )

    assert rows[0] == ("State", "Issue Count")
    assert rows[1] == ("Open", 3)


# Tests that estimate values are used when the y axis is estimate.
def test_NonSegmentedRowsUsesEstimateForEstimateAxis():
    rows = generate_non_segmented_rows(
        distribution={"Open": [{"count": 3, "estimate": 12}]},
        x_axis="state__name",
        y_axis="estimate",
        key="estimate",
        details_by_dimension={},
    )

    assert rows[1] == ("Open", 12)


# Tests that segmented rows contain the total and individual segment values.
def test_SegmentedRowsGeneratesTotalsAndSegmentValues():
    rows = generate_segmented_rows(
        distribution={
            "Open": [
                {"count": 3, "segment": "Bug"},
                {"count": 2, "segment": "Task"},
            ]
        },
        x_axis="state__name",
        y_axis="issue_count",
        segment=LABEL_ID,
        key="count",
        details_by_dimension={LABEL_ID: []},
    )

    header = rows[0]
    data_row = rows[1]

    assert header[:2] == ("State", "Issue Count")
    assert set(header[2:]) == {"Bug", "Task"}
    assert data_row[0] == "Open"
    assert data_row[1] == 5
    assert set(data_row[2:]) == {2, 3}


# Tests that the x axis ID is converted into its readable display name.
def test_SegmentedRowsResolvesXAxisDisplayName():
    rows = generate_segmented_rows(
        distribution={
            "1": [{"count": 4, "segment": "Bug"}]
        },
        x_axis=LABEL_ID,
        y_axis="issue_count",
        segment=STATE_ID,
        key="count",
        details_by_dimension={
            LABEL_ID: [{LABEL_ID: "1", "labels__name": "Bug"}],
            STATE_ID: [],
        },
    )

    assert rows[1][0] == "Bug"


# Tests that rows are correctly converted into CSV content.
def test_GenerateCSVFromRowsCreatesCSVBuffer():
    rows = [
        ["State", "Issue Count"],
        ["Open", 5],
        ["In Progress", 3],
    ]

    csv_buffer = generate_csv_from_rows(rows)

    csv_buffer.seek(0)
    result = list(csv.reader(csv_buffer))

    assert result == [
        ["State", "Issue Count"],
        ["Open", "5"],
        ["In Progress", "3"],
    ]


# Tests that the dimension details helper retrieves only the selected dimensions.
def test_GetAnalyticsDimensionDetailsReturnsSelectedDimensions(monkeypatch):
    monkeypatch.setattr(
        analytic_export, "get_state_details", lambda slug, filters: ["states"]
    )
    monkeypatch.setattr(
        analytic_export, "get_module_details", lambda slug, filters: ["modules"]
    )

    details = analytic_export.get_analytics_dimension_details(
        "workspace", {}, STATE_ID, MODULE_ID
    )

    assert details[STATE_ID] == ["states"]
    assert details[MODULE_ID] == ["modules"]
    assert details[ASSIGNEE_ID] == {}
    assert details[LABEL_ID] == {}
    assert details[CYCLE_ID] == {}


# Tests that no details are retrieved when no dimensions are selected.
def test_GetAnalyticsDimensionDetailsReturnsEmptyDetails(monkeypatch):
    details = analytic_export.get_analytics_dimension_details(
        "workspace", {}, "priority", False
    )

    assert all(value == {} for value in details.values())


# Tests that segmented rows resolve module IDs into module names.
def test_SegmentedRowsResolvesModuleSegmentNames():
    rows = generate_segmented_rows(
        distribution={
            "Open": [{"count": 4, "segment": "module-1"}]
        },
        x_axis="state__name",
        y_axis="issue_count",
        segment=MODULE_ID,
        key="count",
        details_by_dimension={
            MODULE_ID: [
                {
                    MODULE_ID: "module-1",
                    "issue_module__module__name": "Backend",
                }
            ]
        },
    )

    assert rows[0][2] == "Backend"
    assert rows[1][1] == 4


# Tests that the email export sends a CSV attachment.
def test_SendExportEmailAttachesCSV(monkeypatch):
    email_message = MagicMock()
    csv_buffer = io.StringIO("State,Issue Count\nOpen,5")

    monkeypatch.setattr(
        analytic_export, "render_to_string", lambda *args, **kwargs: "<p>Export</p>"
    )
    monkeypatch.setattr(
        analytic_export, "generate_plain_text_from_html", lambda content: "Export"
    )
    monkeypatch.setattr(
        analytic_export,
        "get_email_configuration",
        lambda: ("host", "user", "password", "587", "1", "0", "sender@test.com"),
    )
    monkeypatch.setattr(
        analytic_export, "get_connection", MagicMock(return_value="connection")
    )
    monkeypatch.setattr(
        analytic_export, "EmailMultiAlternatives", MagicMock(return_value=email_message)
    )

    analytic_export.send_export_email(
        "user@test.com", "workspace", csv_buffer, [["State", "Issue Count"]]
    )

    email_message.attach.assert_called_once()
    email_message.send.assert_called_once_with(fail_silently=False)


# Tests that the analytics CSV export prepares rows and sends the email.
def test_ExportAnalyticsToCSVEmailSendsExport(monkeypatch):
    csv_buffer = io.StringIO()
    send_email = MagicMock()

    monkeypatch.setattr(
        analytic_export, "generate_csv_from_rows", lambda rows: csv_buffer
    )
    monkeypatch.setattr(analytic_export, "send_export_email", send_email)

    data = [{"state": "Open", "count": 5}]

    analytic_export.export_analytics_to_csv_email.run(
        data,
        ["State", "Issue Count"],
        ["state", "count"],
        "user@test.com",
        "workspace",
    )

    send_email.assert_called_once()
    assert send_email.call_args.kwargs["slug"] == "workspace"
    assert send_email.call_args.kwargs["rows"] == [
        ["State", "Issue Count"],
        ["Open", 5],
    ]


# Tests that the analytics export task generates and sends nonsegmented rows.
def test_AnalyticExportTaskGeneratesNonSegmentedRows(monkeypatch):
    queryset = MagicMock()
    send_email = MagicMock()
    rows = [("State", "Issue Count"), ("Open", 5)]
    csv_buffer = io.StringIO()

    monkeypatch.setattr(analytic_export, "issue_filters", lambda data, method: {})
    monkeypatch.setattr(
        analytic_export.Issue,
        "issue_objects",
        MagicMock(filter=MagicMock(return_value=queryset)),
    )
    monkeypatch.setattr(
        analytic_export, "build_graph_plot", lambda *args, **kwargs: {"Open": []}
    )
    monkeypatch.setattr(
        analytic_export,
        "get_analytics_dimension_details",
        lambda *args: {},
    )
    monkeypatch.setattr(
        analytic_export, "generate_non_segmented_rows", lambda *args: rows
    )
    monkeypatch.setattr(
        analytic_export, "generate_csv_from_rows", lambda rows: csv_buffer
    )
    monkeypatch.setattr(analytic_export, "send_export_email", send_email)

    analytic_export.analytic_export_task.run(
        "user@test.com",
        {"x_axis": STATE_ID, "y_axis": "issue_count"},
        "workspace",
    )

    send_email.assert_called_once()

# Tests that an error during the analytics export task is logged.
def test_AnalyticExportTaskLogsException(monkeypatch):
    error = Exception("Export failed")

    monkeypatch.setattr(
        analytic_export,
        "issue_filters",
        lambda data, method: {},
    )
    monkeypatch.setattr(
        analytic_export.Issue.issue_objects,
        "filter",
        lambda **kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        analytic_export,
        "build_graph_plot",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        analytic_export,
        "get_analytics_dimension_details",
        lambda *args: {},
    )
    monkeypatch.setattr(
        analytic_export,
        "generate_non_segmented_rows",
        lambda *args: [],
    )
    monkeypatch.setattr(
        analytic_export,
        "generate_csv_from_rows",
        lambda rows: MagicMock(),
    )
    monkeypatch.setattr(
        analytic_export,
        "send_export_email",
        MagicMock(side_effect=error),
    )
    log_mock = MagicMock()
    monkeypatch.setattr(analytic_export, "log_exception", log_mock)

    analytic_export.analytic_export_task.run(
        email="test@example.com",
        data={"x_axis": "state_id", "y_axis": "issue_count"},
        slug="test-workspace",
    )

    log_mock.assert_called_once_with(error)


# Tests that an error during CSV email export is logged.
def test_ExportAnalyticsToCSVEmailLogsException(monkeypatch):
    error = Exception("Email failed")
    send_mock = MagicMock(side_effect=error)
    log_mock = MagicMock()

    monkeypatch.setattr(analytic_export, "send_export_email", send_mock)
    monkeypatch.setattr(analytic_export, "log_exception", log_mock)

    analytic_export.export_analytics_to_csv_email.run(
        data=[{"name": "Test"}],
        headers=["Name"],
        keys=["name"],
        email="test@example.com",
        slug="test-workspace",
    )

    log_mock.assert_called_once_with(error)