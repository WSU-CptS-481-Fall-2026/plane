
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
    rows = generate_non_segmented_rows(
        distribution={"1": [{"count": 5, "estimate": 10}]},
        x_axis=x_axis,
        y_axis="issue_count",
        key="count",
        assignee_details=details if x_axis == ASSIGNEE_ID else [],
        label_details=details if x_axis == LABEL_ID else [],
        state_details=details if x_axis == STATE_ID else [],
        cycle_details=details if x_axis == CYCLE_ID else [],
        module_details=details if x_axis == MODULE_ID else [],
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
        assignee_details=[],
        label_details=[],
        state_details=[],
        cycle_details=[],
        module_details=[],
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
        assignee_details=[],
        label_details=[],
        state_details=[],
        cycle_details=[],
        module_details=[],
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
        assignee_details=[],
        label_details=[],
        state_details=[],
        cycle_details=[],
        module_details=[],
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
        assignee_details=[],
        label_details=[{LABEL_ID: "1", "labels__name": "Bug"}],
        state_details=[],
        cycle_details=[],
        module_details=[],
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
