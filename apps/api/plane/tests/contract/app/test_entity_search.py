import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status

from plane.db.models import Cycle, Issue, Module, Page, Project, ProjectMember, ProjectPage, WorkspaceMember

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture
def search_projects(workspace, create_user):
    primary = Project.objects.create(workspace=workspace, name="Test Primary", identifier="PRI", network=0)
    secondary = Project.objects.create(workspace=workspace, name="Test Secondary", identifier="SEC", network=2)
    hidden = Project.objects.create(workspace=workspace, name="Test Hidden", identifier="HID", network=0)
    for project, is_active in ((primary, True), (secondary, False)):
        ProjectMember.objects.create(
            workspace=workspace, project=project, member=create_user, role=20, is_active=is_active
        )
    return primary, secondary, hidden


@pytest.fixture
def mention_row(workspace, create_user, create_bot_user, search_projects):
    create_user.avatar = "https://example.com/avatar.png"
    create_user.save(update_fields=["avatar"])
    create_bot_user.first_name = "Test Bot"
    create_bot_user.save(update_fields=["first_name"])
    WorkspaceMember.objects.create(workspace=workspace, member=create_bot_user, role=20)
    ProjectMember.objects.create(workspace=workspace, project=search_projects[0], member=create_bot_user, role=20)
    return {
        "member__id": str(create_user.id),
        "member__display_name": create_user.display_name,
        "member__avatar_url": create_user.avatar,
    }


@pytest.fixture
def search_rows(workspace, create_user, search_projects):
    primary, secondary, _ = search_projects
    issue = Issue.objects.create(workspace=workspace, project=primary, name="Test Issue")
    Issue.objects.create(workspace=workspace, project=secondary, name="Test Issue in inactive project")
    cycle = Cycle.objects.create(workspace=workspace, project=primary, name="Test Cycle", owned_by=create_user)
    module = Module.objects.create(workspace=workspace, project=primary, name="Test Module")
    global_page = Page.objects.create(workspace=workspace, owned_by=create_user, name="Test Global", is_global=True)
    project_page = Page.objects.create(workspace=workspace, owned_by=create_user, name="Test Local")
    private_page = Page.objects.create(
        workspace=workspace, owned_by=create_user, name="Test Private", access=Page.PRIVATE_ACCESS, is_global=True
    )
    for page in (global_page, project_page, private_page):
        ProjectPage.objects.create(workspace=workspace, project=primary, page=page)
    ProjectPage.objects.create(workspace=workspace, project=secondary, page=global_page)
    return issue, cycle, module, global_page, project_page


def test_default_mention_search(session_client, workspace, mention_row):
    with CaptureQueriesContext(connection) as queries:
        response = session_client.get(f"/api/workspaces/{workspace.slug}/entity-search/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"user_mention": [mention_row]}
    print(f"workspace default mention: {len(queries)} SQL queries")
    assert len(queries) <= 2


@pytest.mark.parametrize("project_scoped", [False, True], ids=["workspace", "project"])
def test_search_all_types(session_client, workspace, search_projects, mention_row, search_rows, project_scoped):
    primary, secondary, _ = search_projects
    issue, cycle, module, global_page, project_page = search_rows
    params = {"query": "tEsT", "query_type": " user_mention,project,issue,cycle,module,page,unknown "}
    if project_scoped:
        params["project_id"] = str(primary.id)

    with CaptureQueriesContext(connection) as queries:
        response = session_client.get(f"/api/workspaces/{workspace.slug}/entity-search/", params)

    assert response.status_code == status.HTTP_200_OK
    project_fields = {"project_id": str(primary.id), "project__identifier": primary.identifier}
    pages = [project_page, global_page] if project_scoped else [global_page]
    assert response.json() == {
        "user_mention": [mention_row],
        "project": [
            {
                "id": str(project.id),
                "name": project.name,
                "identifier": project.identifier,
                "logo_props": project.logo_props,
                "workspace__slug": workspace.slug,
            }
            for project in (secondary, primary)
        ],
        "issue": [
            {
                "id": str(issue.id),
                "name": issue.name,
                "sequence_id": issue.sequence_id,
                "priority": issue.priority,
                "state_id": str(issue.state_id) if issue.state_id else None,
                "type_id": None,
                **project_fields,
            }
        ],
        "cycle": [
            {
                "id": str(cycle.id),
                "name": cycle.name,
                "status": "DRAFT",
                "workspace__slug": workspace.slug,
                **project_fields,
            }
        ],
        "module": [
            {
                "id": str(module.id),
                "name": module.name,
                "status": module.status,
                "workspace__slug": workspace.slug,
                **project_fields,
            }
        ],
        "page": [
            {
                "id": str(page.id),
                "name": page.name,
                "logo_props": page.logo_props,
                "projects__id": str(primary.id),
                "workspace__slug": workspace.slug,
            }
            for page in pages
        ],
    }
    scope = "project" if project_scoped else "workspace"
    print(f"{scope} all types: {len(queries)} SQL queries")
    assert len(queries) <= 7 # uses 7 queries before refactor


def test_project_scope_excludes_other_active_projects(
    session_client, workspace, create_user, search_projects, search_rows
):
    primary, secondary, _ = search_projects
    issue, cycle, module, _, _ = search_rows
    ProjectMember.objects.filter(project=secondary, member=create_user).update(is_active=True)
    Cycle.objects.create(workspace=workspace, project=secondary, name="Other cycle", owned_by=create_user)
    Module.objects.create(workspace=workspace, project=secondary, name="Other module")

    response = session_client.get(
        f"/api/workspaces/{workspace.slug}/entity-search/",
        {"query_type": "issue,cycle,module", "project_id": str(primary.id)},
    )

    assert response.status_code == status.HTTP_200_OK
    assert {entity: [row["id"] for row in rows] for entity, rows in response.json().items()} == {
        "issue": [str(issue.id)],
        "cycle": [str(cycle.id)],
        "module": [str(module.id)],
    }


def test_issue_search_matches_sequence_and_applies_count(session_client, workspace, search_projects):
    project, _, _ = search_projects
    first = Issue.objects.create(workspace=workspace, project=project, name="First issue")
    second = Issue.objects.create(workspace=workspace, project=project, name="Second issue")
    Issue.objects.create(workspace=workspace, project=project, name="Third issue")

    response = session_client.get(
        f"/api/workspaces/{workspace.slug}/entity-search/",
        {"query_type": "issue", "query": f"{first.sequence_id} {second.sequence_id}", "count": 1},
    )

    assert response.status_code == status.HTTP_200_OK
    assert [row["id"] for row in response.json()["issue"]] == [str(second.id)]


def test_entity_search_requires_active_workspace_membership(session_client, workspace, create_user):
    WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)

    response = session_client.get(f"/api/workspaces/{workspace.slug}/entity-search/")

    assert response.status_code == status.HTTP_403_FORBIDDEN
