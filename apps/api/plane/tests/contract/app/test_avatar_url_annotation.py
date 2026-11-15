# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Regression tests for the ``avatar_url`` annotation used by the assignee
distribution / analytics querysets.

Those querysets build the avatar URL with
``Concat(Value("/api/assets/v2/static/"), <user>__avatar_asset, Value("/"))``.
On Django 5.x ``ConcatPair.as_postgresql`` resolves the output field of each
argument, so concatenating a ``CharField`` with the raw ``UUIDField`` column
raises ``FieldError: Expression contains mixed types``. Every endpoint below
returned a 500 until the UUID column was explicitly cast to text.
"""

from plane.db.models import AnalyticView
import pytest
from django.utils import timezone
from rest_framework import status

from plane.db.models import (
    Cycle,
    CycleIssue,
    FileAsset,
    Issue,
    IssueAssignee,
    Module,
    ModuleIssue,
    Project,
    ProjectMember,
    State,
)


@pytest.fixture
def user_with_avatar_asset(db, workspace, create_user):
    """A user whose avatar comes from a FileAsset, i.e. the branch of the
    ``Case`` expression that concatenates the asset UUID into a URL."""
    asset = FileAsset.objects.create(
        workspace=workspace,
        asset=f"{workspace.id}/avatar.png",
        size=1024,
        entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
        user=create_user,
        is_uploaded=True,
    )
    create_user.avatar_asset = asset
    create_user.save()
    return create_user


@pytest.fixture
def project(db, workspace, user_with_avatar_asset):
    project = Project.objects.create(
        name="Avatar URL Project",
        identifier="AVU",
        workspace=workspace,
        created_by=user_with_avatar_asset,
        module_view=True,
        cycle_view=True,
    )
    ProjectMember.objects.create(
        workspace=workspace,
        project=project,
        member=user_with_avatar_asset,
        role=20,
        is_active=True,
    )
    return project


@pytest.fixture
def assigned_issue(db, project, user_with_avatar_asset):
    """A work item assigned to the avatar-bearing user, so the distribution
    querysets have at least one row to annotate."""
    state = State.objects.create(name="Todo", group="unstarted", project=project, workspace=project.workspace)
    issue = Issue.objects.create(
        name="Avatar URL Issue",
        project=project,
        workspace=project.workspace,
        state=state,
        created_by=user_with_avatar_asset,
    )
    IssueAssignee.objects.create(
        issue=issue,
        assignee=user_with_avatar_asset,
        project=project,
        workspace=project.workspace,
    )
    return issue


@pytest.fixture
def module(db, project, assigned_issue):
    module = Module.objects.create(name="Avatar URL Module", project=project, workspace=project.workspace)
    ModuleIssue.objects.create(issue=assigned_issue, module=module, project=project, workspace=project.workspace)
    return module


@pytest.fixture
def archived_module(db, project, assigned_issue):
    module = Module.objects.create(
        name="Archived Avatar URL Module",
        project=project,
        workspace=project.workspace,
        archived_at=timezone.now(),
    )
    ModuleIssue.objects.create(issue=assigned_issue, module=module, project=project, workspace=project.workspace)
    return module


@pytest.fixture
def cycle(db, project, assigned_issue, user_with_avatar_asset):
    cycle = Cycle.objects.create(
        name="Avatar URL Cycle",
        project=project,
        workspace=project.workspace,
        owned_by=user_with_avatar_asset,
        start_date=timezone.now(),
        end_date=timezone.now() + timezone.timedelta(days=7),
    )
    CycleIssue.objects.create(issue=assigned_issue, cycle=cycle, project=project, workspace=project.workspace)
    return cycle


@pytest.fixture
def archived_cycle(db, project, user_with_avatar_asset):
    return Cycle.objects.create(
        name="Archived Avatar URL Cycle",
        project=project,
        workspace=project.workspace,
        owned_by=user_with_avatar_asset,
        archived_at=timezone.now(),
    )


@pytest.mark.contract
class TestAvatarUrlAnnotation:
    @pytest.mark.django_db
    def test_module_retrieve_builds_avatar_url_from_asset(
        self, session_client, workspace, project, module, user_with_avatar_asset
    ):
        response = session_client.get(f"/api/workspaces/{workspace.slug}/projects/{project.id}/modules/{module.id}/")
        assert response.status_code == status.HTTP_200_OK

        assignees = list(response.data["distribution"]["assignees"])
        assert [a["avatar_url"] for a in assignees] == [
            f"/api/assets/v2/static/{user_with_avatar_asset.avatar_asset_id}/"
        ]

    @pytest.mark.django_db
    def test_archived_module_retrieve(self, session_client, workspace, project, archived_module):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/archived-modules/{archived_module.id}/"
        )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_archived_cycle_retrieve(self, session_client, workspace, project, archived_cycle):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/archived-cycles/{archived_cycle.id}/"
        )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_cycle_analytics(self, session_client, workspace, project, cycle):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/cycles/{cycle.id}/analytics/?type=issues"
        )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_workspace_analytics(self, session_client, workspace, project, assigned_issue):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/?x_axis=assignees__id&y_axis=issue_count"
        )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_default_analytics(self, session_client, workspace, project, assigned_issue):
        response = session_client.get(f"/api/workspaces/{workspace.slug}/default-analytics/")
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_project_advance_analytics_stats(self, session_client, workspace, project, assigned_issue):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/advance-analytics-stats/?type=work-items"
        )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_entity_search_user_mention(self, session_client, workspace, project):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/entity-search/"
            f"?query_type=user_mention&query=Test&project_id={project.id}"
        )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.django_db
    def test_cycle_transfer_issues(self, session_client, workspace, project, cycle, user_with_avatar_asset):
        new_cycle = Cycle.objects.create(
            name="Transfer Target Cycle",
            project=project,
            workspace=project.workspace,
            owned_by=user_with_avatar_asset,
            start_date=timezone.now() + timezone.timedelta(days=8),
            end_date=timezone.now() + timezone.timedelta(days=14),
        )
        response = session_client.post(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/cycles/{cycle.id}/transfer-issues/",
            {"new_cycle_id": str(new_cycle.id)},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK

@pytest.mark.contract
class TestProjectStatsEndpoint:
    
    # Tests that all five project statistics are returned with the correct counts.
    @pytest.mark.django_db
    def test_project_stats_returns_all_statistics(
        self, session_client, workspace, project, assigned_issue, module, cycle
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/project-stats/"
        )

        assert response.status_code == status.HTTP_200_OK

        project_stats = next(
            item for item in response.data if str(item["id"]) == str(project.id)
        )

        assert project_stats["total_issues"] == 1
        assert project_stats["completed_issues"] == 0
        assert project_stats["total_members"] == 1
        assert project_stats["total_cycles"] == 1
        assert project_stats["total_modules"] == 1

    # Tests that only the requested project statistics are returned.        
    @pytest.mark.django_db
    def test_project_stats_returns_only_requested_fields(
        self, session_client, workspace, project, assigned_issue
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/project-stats/?fields=total_issues,total_members"
        )

        assert response.status_code == status.HTTP_200_OK

        project_stats = response.data[0]

        assert project_stats["total_issues"] == 1
        assert project_stats["total_members"] == 1
        assert "completed_issues" not in project_stats
        assert "total_cycles" not in project_stats
        assert "total_modules" not in project_stats
    
    # Tests that project statistics can be filtered by project IDs.
    @pytest.mark.django_db
    def test_project_stats_filters_by_project_ids(
        self, session_client, workspace, project
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/project-stats/?project_ids={project.id}"
        )

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert str(response.data[0]["id"]) == str(project.id)
        
@pytest.mark.contract
class TestAnalyticsValidation:
    
    # Tests that analytics returns an error when required dimensions are missing.
    @pytest.mark.django_db
    def test_analytics_returns_error_for_missing_dimensions(
        self, session_client, workspace
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"] == (
            "x-axis and y-axis dimensions are required and the values should be valid"
        )
    
    # Tests that analytics rejects a segment matching the x-axis dimension.
    @pytest.mark.django_db
    def test_analytics_returns_error_for_matching_segment_and_x_axis(
        self, session_client, workspace
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/"
            "?x_axis=state_id&y_axis=issue_count&segment=state_id"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"] == (
            "Both segment and x axis cannot be same and segment should be valid"
        )
        
    # Tests that analytics successfully returns issue totals, distribution, and state details.   
    @pytest.mark.django_db
    def test_analytics_returns_successful_response(
        self, session_client, workspace, assigned_issue
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/"
            "?x_axis=state_id&y_axis=issue_count"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["total"] == 1
        assert "distribution" in response.data
        assert "extras" in response.data
        assert response.data["extras"]["state_details"]
  
    # Tests that analytics returns cycle details for the selected cycle dimension.         
    @pytest.mark.django_db
    def test_analytics_returns_cycle_details(
        self, session_client, workspace, assigned_issue, cycle
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/"
            "?x_axis=issue_cycle__cycle_id&y_axis=issue_count"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["extras"]["cycle_details"]
    
    # Tests that analytics returns module details for the selected module dimension.    
    @pytest.mark.django_db
    def test_analytics_returns_module_details(
        self, session_client, workspace, assigned_issue, module
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/"
            "?x_axis=issue_module__module_id&y_axis=issue_count"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["extras"]["module_details"]
    
    # Tests that analytics returns assignee details for the selected assignee dimension.
    @pytest.mark.django_db
    def test_analytics_returns_assignee_details(
        self, session_client, workspace, assigned_issue
    ):
        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/analytics/"
            "?x_axis=assignees__id&y_axis=issue_count"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["extras"]["assignee_details"]
        
    # Tests that analytics export rejects requests with missing dimensions.
    @pytest.mark.django_db
    def test_export_analytics_returns_error_for_missing_dimensions(
        self, session_client, workspace
    ):
        response = session_client.post(
            f"/api/workspaces/{workspace.slug}/export-analytics/",
            data={},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"] == (
            "x-axis and y-axis dimensions are required and the values should be valid"
        )
    
    # Tests that analytics export successfully queues an export request.    
    @pytest.mark.django_db
    def test_export_analytics_success(
        self, session_client, workspace
    ):
        response = session_client.post(
            f"/api/workspaces/{workspace.slug}/export-analytics/",
            data={
                "x_axis": "state_id",
                "y_axis": "issue_count",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert "Once the export is ready" in response.data["message"]

    # Tests that a saved analytics view returns its issue total and distribution.
    @pytest.mark.django_db
    def test_saved_analytics_returns_successful_response(
        self, session_client, workspace, assigned_issue
    ):
        analytic_view = AnalyticView.objects.create(
            workspace=workspace,
            name="Test Analytics",
            query={"workspace__slug": workspace.slug},
            query_dict={
                "x_axis": "state_id",
                "y_axis": "issue_count",
            },
        )

        response = session_client.get(
            f"/api/workspaces/{workspace.slug}/saved-analytic-view/{analytic_view.id}/"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["total"] == 1
        assert "distribution" in response.data
    
    # Tests that creating an analytics view assigns it to the correct workspace.    
    @pytest.mark.django_db
    def test_analytic_view_creation_assigns_workspace(
        self, session_client, workspace
    ):
        response = session_client.post(
            f"/api/workspaces/{workspace.slug}/analytic-view/",
            data={
                "name": "Test Analytics",
                "query_dict": {},
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert str(response.data["workspace"]) == str(workspace.id)