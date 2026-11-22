# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for ``WorkspaceFileAssetEndpoint`` project-scoping.

Regression coverage for GHSA-h7mc-p9mm-2r4w / GHSA-cjph-cgm5-8pw8 (WEB-8066),
an incomplete fix of the GHSA-qw87 asset-IDOR cluster.

The endpoint is authorized at the WORKSPACE level, so any workspace member or
guest previously reached ``get``/``patch``/``delete`` for a project-bound asset
(issue attachment / description, comment description, page description) even
when they were not a member of that asset's project. The fix requires an active
``ProjectMember`` of ``asset.project_id`` for project-bound assets, while
leaving workspace-level assets (WORKSPACE_LOGO, USER_AVATAR, USER_COVER, whose
``project_id`` is NULL) accessible to any workspace member.
"""

from types import SimpleNamespace
from unittest import mock
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import IntegrityError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from plane.app.views.asset.v2 import (
    AssetRestoreEndpoint,
    DuplicateAssetEndpoint,
    ProjectAssetEndpoint,
    ProjectBulkAssetEndpoint,
    UserAssetsV2Endpoint,
    WorkspaceFileAssetEndpoint,
)
from plane.db.models import (
    FileAsset,
    Project,
    ProjectMember,
    User,
    WorkspaceMember,
)


S3_STORAGE_PATH = "plane.app.views.asset.v2.S3Storage"


@pytest.fixture
def project(db, workspace, create_user):
    """A project in the fixture workspace; ``create_user`` is an active member."""
    project = Project.objects.create(
        name="Test Project",
        identifier="TP",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20
    )
    return project


@pytest.fixture
def outsider_user(db):
    """A user who is a workspace member but NOT a member of ``project``."""
    unique_id = uuid4().hex[:8]
    user = User.objects.create(
        email=f"outsider-{unique_id}@plane.so",
        username=f"outsider_{unique_id}",
        first_name="Outsider",
        last_name="User",
    )
    user.set_password("test-password")
    user.save()
    return user


@pytest.fixture
def outsider_client(db, workspace, outsider_user):
    """Session client for a workspace member who is not in ``project``."""
    WorkspaceMember.objects.create(
        workspace=workspace, member=outsider_user, role=15
    )
    client = APIClient()
    client.force_authenticate(user=outsider_user)
    return client


@pytest.fixture
def project_asset(db, workspace, project, create_user):
    """An uploaded issue attachment that belongs to ``project``."""
    return FileAsset.objects.create(
        attributes={"name": "secret.pdf", "type": "application/pdf", "size": 1024},
        asset=f"{workspace.id}/secret.pdf",
        size=1024,
        workspace=workspace,
        project=project,
        created_by=create_user,
        entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
        is_uploaded=True,
        storage_metadata={"size": 1024},
    )


@pytest.fixture
def workspace_logo_asset(db, workspace, create_user):
    """A workspace-level asset (project_id is NULL) — exempt from project scope."""
    return FileAsset.objects.create(
        attributes={"name": "logo.png", "type": "image/png", "size": 256},
        asset=f"{workspace.id}/logo.png",
        size=256,
        workspace=workspace,
        created_by=create_user,
        entity_type=FileAsset.EntityTypeContext.WORKSPACE_LOGO,
        is_uploaded=True,
        storage_metadata={"size": 256},
    )


def detail_url(slug, asset_id):
    return f"/api/assets/v2/workspaces/{slug}/{asset_id}/"


@pytest.mark.contract
class TestWorkspaceFileAssetProjectScope:
    """A workspace member who is not in the asset's project must be blocked."""

    @pytest.mark.django_db
    def test_get_project_asset_denied_for_non_project_member(
        self, outsider_client, workspace, project_asset
    ):
        """GET on a project asset by a non-project-member must 403, not mint a
        presigned download URL."""
        url = detail_url(workspace.slug, project_asset.id)

        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://signed.example/download"
            )
            response = outsider_client.get(url)

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Got {response.status_code}: {getattr(response, 'data', None)!r}"
        )
        mock_storage.return_value.generate_presigned_url.assert_not_called()

    @pytest.mark.django_db
    def test_patch_project_asset_denied_for_non_project_member(
        self, outsider_client, workspace, project_asset
    ):
        """PATCH on a project asset by a non-project-member must 403 and leave
        the asset untouched."""
        url = detail_url(workspace.slug, project_asset.id)
        project_asset.is_uploaded = False
        project_asset.save(update_fields=["is_uploaded"])

        response = outsider_client.patch(
            url, {"attributes": {"name": "hacked.pdf"}}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Got {response.status_code}: {getattr(response, 'data', None)!r}"
        )
        project_asset.refresh_from_db()
        assert project_asset.is_uploaded is False
        assert project_asset.attributes.get("name") == "secret.pdf"

    @pytest.mark.django_db
    def test_delete_project_asset_denied_for_non_project_member(
        self, outsider_client, workspace, project_asset
    ):
        """DELETE on a project asset by a non-project-member must 403 and must
        not soft-delete the asset."""
        url = detail_url(workspace.slug, project_asset.id)

        response = outsider_client.delete(url)

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Got {response.status_code}: {getattr(response, 'data', None)!r}"
        )
        project_asset.refresh_from_db()
        assert project_asset.is_deleted is False

    @pytest.mark.django_db
    def test_get_project_asset_allowed_for_project_member(
        self, session_client, workspace, project_asset
    ):
        """Positive control: an active project member can still download the
        asset, so the fix does not over-block legitimate callers."""
        url = detail_url(workspace.slug, project_asset.id)

        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://signed.example/download"
            )
            response = session_client.get(url)

        assert response.status_code == status.HTTP_302_FOUND, (
            f"Got {response.status_code}: {getattr(response, 'data', None)!r}"
        )
        mock_storage.return_value.generate_presigned_url.assert_called_once()

    @pytest.mark.django_db
    def test_get_workspace_level_asset_allowed_for_non_project_member(
        self, outsider_client, workspace, workspace_logo_asset
    ):
        """Exemption control: a workspace-level asset (project_id NULL) stays
        accessible to any workspace member."""
        url = detail_url(workspace.slug, workspace_logo_asset.id)

        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://signed.example/download"
            )
            response = outsider_client.get(url)

        assert response.status_code == status.HTTP_302_FOUND, (
            f"Got {response.status_code}: {getattr(response, 'data', None)!r}"
        )
        mock_storage.return_value.generate_presigned_url.assert_called_once()


# Check that each entity type updates the correct asset fields.
@pytest.mark.parametrize(
    ("entity_type", "expected_fields"),
    [
        (
            FileAsset.EntityTypeContext.ISSUE_DESCRIPTION,
            {"issue_id": "entity-1", "project_id": "project-1"},
        ),
        (
            FileAsset.EntityTypeContext.COMMENT_DESCRIPTION,
            {"comment_id": "entity-1"},
        ),
        (
            FileAsset.EntityTypeContext.PAGE_DESCRIPTION,
            {"page_id": "entity-1"},
        ),
        (
            FileAsset.EntityTypeContext.DRAFT_ISSUE_DESCRIPTION,
            {"draft_issue_id": "entity-1"},
        ),
    ],
)
def test_UpdateAssetEntityUpdatesCorrectFields(
    entity_type, expected_fields
):
    endpoint = ProjectBulkAssetEndpoint()
    assets = mock.Mock()

    endpoint.update_asset_entity(
        assets, entity_type, "entity-1", "project-1"
    )

    assets.update.assert_called_once_with(**expected_fields)


# Check that unsupported entity types are ignored.
def test_UpdateAssetEntityIgnoresUnsupportedEntityType():
    endpoint = ProjectBulkAssetEndpoint()
    assets = mock.Mock()

    endpoint.update_asset_entity(
        assets, "UNSUPPORTED", "entity-1", "project-1"
    )

    assets.update.assert_not_called()


# Check that project cover assets are assigned to the project.
def test_UpdateAssetEntityHandlesProjectCover():
    endpoint = ProjectBulkAssetEndpoint()
    asset1 = mock.Mock()
    asset2 = mock.Mock()
    assets = mock.MagicMock()
    assets.__iter__.return_value = [asset1, asset2]
    project = mock.Mock()

    with (
        mock.patch(
            "plane.app.views.asset.v2.Project.objects.get",
            return_value=project,
        ) as get_project,
        mock.patch.object(endpoint, "save_project_cover") as save_project_cover,
    ):
        endpoint.update_asset_entity(
            assets,
            FileAsset.EntityTypeContext.PROJECT_COVER,
            "entity-1",
            "project-1",
        )

    assets.update.assert_called_once_with(project_id="project-1")
    get_project.assert_called_once_with(id="project-1")
    assert save_project_cover.call_args_list == [
        mock.call(asset1, project),
        mock.call(asset2, project),
    ]


# Check that each endpoint maps entity types to the correct ID field.
@pytest.mark.parametrize(
    ("endpoint_class", "entity_type", "expected_fields"),
    [
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.WORKSPACE_LOGO, {"workspace_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.PROJECT_COVER, {"project_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.USER_AVATAR, {"user_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.USER_COVER, {"user_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.ISSUE_ATTACHMENT, {"issue_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.ISSUE_DESCRIPTION, {"issue_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.PAGE_DESCRIPTION, {"page_id": "entity-1"}),
        (DuplicateAssetEndpoint, FileAsset.EntityTypeContext.COMMENT_DESCRIPTION, {"comment_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.WORKSPACE_LOGO, {"workspace_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.PROJECT_COVER, {"project_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.USER_AVATAR, {"user_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.USER_COVER, {"user_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.ISSUE_ATTACHMENT, {"issue_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.ISSUE_DESCRIPTION, {"issue_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.PAGE_DESCRIPTION, {"page_id": "entity-1"}),
        (WorkspaceFileAssetEndpoint, FileAsset.EntityTypeContext.COMMENT_DESCRIPTION, {"comment_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.WORKSPACE_LOGO, {"workspace_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.PROJECT_COVER, {"project_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.USER_AVATAR, {"user_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.USER_COVER, {"user_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.ISSUE_ATTACHMENT, {"issue_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.ISSUE_DESCRIPTION, {"issue_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.PAGE_DESCRIPTION, {"page_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.COMMENT_DESCRIPTION, {"comment_id": "entity-1"}),
        (ProjectAssetEndpoint, FileAsset.EntityTypeContext.DRAFT_ISSUE_DESCRIPTION, {"draft_issue_id": "entity-1"}),
    ],
)
def test_GetEntityIdFieldReturnsCorrectField(
    endpoint_class, entity_type, expected_fields
):
    endpoint = endpoint_class()
    assert endpoint.get_entity_id_field(entity_type, "entity-1") == expected_fields


# Check that a workspace asset is created and upload data is returned.
@pytest.mark.django_db
def test_WorkspaceAssetPostCreatesAssetAndReturnsUploadData(
    session_client, create_user, workspace
):
    url = f"/api/assets/v2/workspaces/{workspace.slug}/"
    payload = {
        "name": "avatar.png",
        "type": "image/png",
        "size": 1024,
        "entity_type": FileAsset.EntityTypeContext.USER_AVATAR,
        "entity_identifier": str(create_user.id),
    }

    with mock.patch(S3_STORAGE_PATH) as mock_storage:
        mock_storage.return_value.generate_presigned_post.return_value = {
            "url": "https://example.com/upload"
        }
        response = session_client.post(url, payload, format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.data["asset_id"]
    assert response.data["upload_data"] == {"url": "https://example.com/upload"}
    assert response.data["asset_url"]

    asset = FileAsset.objects.get(id=response.data["asset_id"])
    assert asset.workspace == workspace
    assert asset.created_by == create_user
    assert asset.entity_type == FileAsset.EntityTypeContext.USER_AVATAR
    assert asset.user_id == create_user.id
    assert asset.attributes["name"] == "avatar.png"
    assert asset.attributes["type"] == "image/png"
    assert asset.size == 1024
    mock_storage.return_value.generate_presigned_post.assert_called_once()


# Check that a project asset is created and upload data is returned.
@pytest.mark.django_db
def test_ProjectAssetPostCreatesAssetAndReturnsUploadData(
    create_user, workspace, project
):
    payload = {
        "name": "avatar.png",
        "type": "image/png",
        "size": 1024,
        "entity_type": FileAsset.EntityTypeContext.USER_AVATAR,
        "entity_identifier": str(create_user.id),
    }
    request = APIRequestFactory().post("/api/assets/v2/", payload, format="json")
    force_authenticate(request, user=create_user)

    with mock.patch(S3_STORAGE_PATH) as mock_storage:
        mock_storage.return_value.generate_presigned_post.return_value = {
            "url": "https://example.com/upload"
        }
        response = ProjectAssetEndpoint.as_view()(
            request, slug=workspace.slug, project_id=project.id
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["asset_id"]
    assert response.data["upload_data"] == {"url": "https://example.com/upload"}
    assert response.data["asset_url"]

    asset = FileAsset.objects.get(id=response.data["asset_id"])
    assert asset.workspace == workspace
    assert asset.project_id == project.id
    assert asset.entity_type == FileAsset.EntityTypeContext.USER_AVATAR
    assert asset.user_id == create_user.id
    assert asset.attributes["name"] == "avatar.png"
    assert asset.attributes["type"] == "image/png"
    assert asset.size == 1024
    mock_storage.return_value.generate_presigned_post.assert_called_once()


# Check that a bulk asset request is rejected when no asset IDs are provided.
@pytest.mark.django_db
def test_ProjectBulkAssetPostRejectsMissingAssetIds(
    create_user, workspace, project
):
    request = APIRequestFactory().post("/api/assets/v2/bulk/", {}, format="json")
    force_authenticate(request, user=create_user)

    response = ProjectBulkAssetEndpoint.as_view()(
        request,
        slug=workspace.slug,
        project_id=project.id,
        entity_id=uuid4(),
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# Check that the duplicate asset endpoint creates and returns a duplicate.
@pytest.mark.django_db
def test_DuplicateAssetPostCreatesAsset(
    create_user, workspace, project_asset
):
    payload = {
        "entity_type": FileAsset.EntityTypeContext.USER_AVATAR,
        "entity_id": str(create_user.id),
    }
    request = APIRequestFactory().post(
        "/api/assets/v2/duplicate/", payload, format="json"
    )
    force_authenticate(request, user=create_user)

    with mock.patch("plane.app.views.asset.v2.S3Storage") as mock_storage:
        response = DuplicateAssetEndpoint.as_view()(
            request, slug=workspace.slug, asset_id=project_asset.id
        )

    assert response.status_code == status.HTTP_200_OK
    duplicated_asset = FileAsset.objects.get(id=response.data["asset_id"])
    assert duplicated_asset.id != project_asset.id
    assert duplicated_asset.workspace == workspace
    assert duplicated_asset.entity_type == FileAsset.EntityTypeContext.USER_AVATAR
    assert duplicated_asset.user_id == create_user.id
    assert duplicated_asset.is_uploaded is True
    mock_storage.return_value.copy_object.assert_called_once()


# Check that a project member can update an uploaded asset.
@pytest.mark.django_db
def test_WorkspaceFileAssetPatchUpdatesAsset(
    session_client, workspace, project_asset
):
    response = session_client.patch(
        f"/api/assets/v2/workspaces/{workspace.slug}/{project_asset.id}/",
        {"attributes": {"name": "updated.pdf"}},
        format="json",
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    project_asset.refresh_from_db()
    assert project_asset.attributes["name"] == "updated.pdf"
    assert project_asset.is_uploaded is True


# Check that a project member can delete an uploaded asset.
@pytest.mark.django_db
def test_WorkspaceFileAssetDeleteMarksAssetDeleted(
    session_client, workspace, project_asset
):
    response = session_client.delete(
        f"/api/assets/v2/workspaces/{workspace.slug}/{project_asset.id}/"
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    project_asset.refresh_from_db()
    assert project_asset.is_deleted is True
    assert project_asset.deleted_at is not None


# Check that a deleted asset can be restored.
@pytest.mark.django_db
def test_AssetRestoreRestoresDeletedAsset(
    create_user, workspace, project_asset
):
    project_asset.is_deleted = True
    project_asset.deleted_at = timezone.now()
    project_asset.save()

    request = APIRequestFactory().post(
        f"/api/assets/v2/restore/{project_asset.id}/"
    )
    force_authenticate(request, user=create_user)
    response = AssetRestoreEndpoint.as_view()(
        request, slug=workspace.slug, asset_id=project_asset.id
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    project_asset.refresh_from_db()
    assert project_asset.is_deleted is False
    assert project_asset.deleted_at is None


# Check that saving a user avatar or cover updates the user and deletes the previous asset.
@pytest.mark.parametrize(
    "entity_type,asset_field,image_field,image_value,previous_asset_id",
    [
        (FileAsset.EntityTypeContext.USER_AVATAR, "avatar_asset_id", "avatar", "", "old-avatar"),
        (FileAsset.EntityTypeContext.USER_COVER, "cover_image_asset_id", "cover_image", None, "old-cover"),
        (FileAsset.EntityTypeContext.USER_AVATAR, "avatar_asset_id", "avatar", "", None),
        (FileAsset.EntityTypeContext.USER_COVER, "cover_image_asset_id", "cover_image", None, None),
    ],
)
def test_UserAssetEntitySaveUpdatesUser(
    entity_type, asset_field, image_field, image_value, previous_asset_id
):
    endpoint = UserAssetsV2Endpoint()
    user = mock.Mock()
    setattr(user, asset_field, previous_asset_id)
    request = mock.Mock()

    with (
        mock.patch("plane.app.views.asset.v2.User.objects.get", return_value=user),
        mock.patch.object(endpoint, "asset_delete") as delete_asset,
        mock.patch.object(endpoint, "invalidate_user_asset_cache") as invalidate_cache,
    ):
        endpoint.entity_asset_save(
            asset_id="new-asset",
            entity_type=entity_type,
            asset=mock.Mock(user_id="user-id"),
            request=request,
        )

    assert getattr(user, asset_field) == "new-asset"
    assert getattr(user, image_field) == image_value
    user.save.assert_called_once()
    if previous_asset_id:
        delete_asset.assert_called_once_with(previous_asset_id)
    else:
        delete_asset.assert_not_called()
    invalidate_cache.assert_called_once_with(request)


# Check that unsupported entity types do not update a user asset.
def test_UserAssetEntitySaveIgnoresUnsupportedType():
    endpoint = UserAssetsV2Endpoint()
    with mock.patch("plane.app.views.asset.v2.User.objects.get") as get_user:
        endpoint.entity_asset_save(
            asset_id="new-asset",
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            asset=mock.Mock(user_id="user-id"),
            request=mock.Mock(),
        )
    get_user.assert_not_called()


# Check that deleting a user avatar or cover clears its asset reference.
@pytest.mark.parametrize(
    "entity_type,asset_field",
    [
        (FileAsset.EntityTypeContext.USER_AVATAR, "avatar_asset_id"),
        (FileAsset.EntityTypeContext.USER_COVER, "cover_image_asset_id"),
    ],
)
def test_UserAssetEntityDeleteClearsUserReference(entity_type, asset_field):
    endpoint = UserAssetsV2Endpoint()
    user = mock.Mock()
    setattr(user, asset_field, "old-asset")

    with (
        mock.patch("plane.app.views.asset.v2.User.objects.get", return_value=user),
        mock.patch("plane.app.views.asset.v2.invalidate_cache_directly") as invalidate_cache,
    ):
        endpoint.entity_asset_delete(
            entity_type=entity_type,
            asset=mock.Mock(user_id="user-id"),
            request=mock.Mock(),
        )

    assert getattr(user, asset_field) is None
    user.save.assert_called_once()
    assert invalidate_cache.call_count == 2


# Check that unsupported entity types do not clear a user asset.
def test_UserAssetEntityDeleteIgnoresUnsupportedType():
    endpoint = UserAssetsV2Endpoint()
    with mock.patch("plane.app.views.asset.v2.User.objects.get") as get_user:
        endpoint.entity_asset_delete(
            entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            asset=mock.Mock(user_id="user-id"),
            request=mock.Mock(),
        )
    get_user.assert_not_called()


# Check that deleting a missing asset does not cause an error.
@pytest.mark.django_db
def test_UserAssetDeleteIgnoresMissingAsset():
    UserAssetsV2Endpoint().asset_delete(uuid4())


# Check that unsupported workspace and project entity types return empty mappings.
@pytest.mark.parametrize(
    "endpoint_class",
    [WorkspaceFileAssetEndpoint, ProjectAssetEndpoint, DuplicateAssetEndpoint],
)
def test_AssetEntityIdFieldUnsupportedType(endpoint_class):
    endpoint = endpoint_class()
    assert endpoint.get_entity_id_field("INVALID_TYPE", "entity-1") == {}


# Check that saving a workspace logo updates the asset and invalidates caches.
def test_WorkspaceEntityAssetSaveUpdatesLogo():
    endpoint = WorkspaceFileAssetEndpoint()
    workspace = mock.Mock()

    with (
        mock.patch("plane.app.views.asset.v2.Workspace.objects.filter") as filter_workspace,
        mock.patch.object(endpoint, "update_entity_asset") as update_asset,
        mock.patch("plane.app.views.asset.v2.invalidate_cache_directly") as invalidate_cache,
    ):
        filter_workspace.return_value.first.return_value = workspace
        endpoint.entity_asset_save(
            asset_id="new-asset",
            entity_type=FileAsset.EntityTypeContext.WORKSPACE_LOGO,
            asset=mock.Mock(workspace_id="workspace-1"),
            request=mock.Mock(),
        )

    update_asset.assert_called_once_with(
        workspace, "new-asset", "logo_asset_id", "logo"
    )
    assert invalidate_cache.call_count == 3


# Check that saving a project cover updates the asset without invalidating workspace caches.
def test_WorkspaceEntityAssetSaveUpdatesProjectCover():
    endpoint = WorkspaceFileAssetEndpoint()
    project = mock.Mock()

    with (
        mock.patch("plane.app.views.asset.v2.Project.objects.filter") as filter_project,
        mock.patch.object(endpoint, "update_entity_asset") as update_asset,
        mock.patch("plane.app.views.asset.v2.invalidate_cache_directly") as invalidate_cache,
    ):
        filter_project.return_value.first.return_value = project
        endpoint.entity_asset_save(
            asset_id="new-asset",
            entity_type=FileAsset.EntityTypeContext.PROJECT_COVER,
            asset=mock.Mock(project_id="project-1"),
            request=mock.Mock(),
        )

    update_asset.assert_called_once_with(
        project, "new-asset", "cover_image_asset_id", "cover_image"
    )
    invalidate_cache.assert_not_called()


# Check that saving an asset does nothing when its workspace or project is missing.
@pytest.mark.parametrize(
    "entity_type,model_path,asset_field",
    [
        (
            FileAsset.EntityTypeContext.WORKSPACE_LOGO,
            "plane.app.views.asset.v2.Workspace.objects.filter",
            "workspace_id",
        ),
        (
            FileAsset.EntityTypeContext.PROJECT_COVER,
            "plane.app.views.asset.v2.Project.objects.filter",
            "project_id",
        ),
    ],
)
def test_WorkspaceEntityAssetSaveIgnoresMissingEntity(
    entity_type, model_path, asset_field
):
    endpoint = WorkspaceFileAssetEndpoint()
    with (
        mock.patch(model_path) as filter_entity,
        mock.patch.object(endpoint, "update_entity_asset") as update_asset,
    ):
        filter_entity.return_value.first.return_value = None
        endpoint.entity_asset_save(
            asset_id="new-asset",
            entity_type=entity_type,
            asset=mock.Mock(**{asset_field: "missing-id"}),
            request=mock.Mock(),
        )
    update_asset.assert_not_called()


# Check that bulk asset updates ignore integrity errors.
def test_ProjectBulkAssetUpdateIgnoresIntegrityErrors():
    endpoint = ProjectBulkAssetEndpoint()
    assets = mock.Mock()
    assets.update.side_effect = IntegrityError

    endpoint.update_asset_entity(
        assets,
        FileAsset.EntityTypeContext.ISSUE_DESCRIPTION,
        "entity-1",
        "project-1",
    )

    assets.update.assert_called_once()

# Check that user asset patch marks the asset uploaded and updates its attributes.
@pytest.mark.parametrize("storage_metadata", [{}, {"size": 100}])
def test_UserAssetPatchUpdatesAsset(monkeypatch, storage_metadata):
    asset = Mock()
    asset.storage_metadata = storage_metadata
    asset.attributes = {"name": "old"}
    asset.entity_type = FileAsset.EntityTypeContext.USER_AVATAR

    request = SimpleNamespace(
        user=SimpleNamespace(id="user-1"),
        data={"attributes": {"name": "new"}},
    )

    monkeypatch.setattr(FileAsset.objects, "get", lambda **kwargs: asset)

    metadata_task = Mock()
    monkeypatch.setattr(
        "plane.app.views.asset.v2.get_asset_object_metadata.delay",
        metadata_task,
    )

    endpoint = UserAssetsV2Endpoint()
    endpoint.entity_asset_save = Mock()

    response = endpoint.patch(request, "asset-1")

    assert response.status_code == 204
    assert asset.is_uploaded is True
    assert asset.attributes == {"name": "new"}
    asset.save.assert_called_once_with(update_fields=["is_uploaded", "attributes"])
    endpoint.entity_asset_save.assert_called_once()
    assert metadata_task.called is (not bool(storage_metadata))


# Check that user asset patch preserves existing attributes when none are supplied.
def test_UserAssetPatchPreservesAttributes(monkeypatch):
    asset = Mock()
    asset.storage_metadata = {"size": 100}
    asset.attributes = {"name": "existing"}
    asset.entity_type = FileAsset.EntityTypeContext.USER_COVER

    request = SimpleNamespace(
        user=SimpleNamespace(id="user-1"),
        data={},
    )

    monkeypatch.setattr(FileAsset.objects, "get", lambda **kwargs: asset)

    endpoint = UserAssetsV2Endpoint()
    endpoint.entity_asset_save = Mock()

    response = endpoint.patch(request, "asset-1")

    assert response.status_code == 204
    assert asset.attributes == {"name": "existing"}
    endpoint.entity_asset_save.assert_called_once()


# Check that user asset delete marks the asset deleted and clears its entity reference.
def test_UserAssetDeleteMarksAssetDeleted(monkeypatch):
    asset = Mock()
    asset.entity_type = FileAsset.EntityTypeContext.USER_AVATAR

    request = SimpleNamespace(user=SimpleNamespace(id="user-1"))

    monkeypatch.setattr(FileAsset.objects, "get", lambda **kwargs: asset)

    endpoint = UserAssetsV2Endpoint()
    endpoint.entity_asset_delete = Mock()

    response = endpoint.delete(request, "asset-1")

    assert response.status_code == 204
    assert asset.is_deleted is True
    assert asset.deleted_at is not None
    endpoint.entity_asset_delete.assert_called_once_with(
        entity_type=asset.entity_type,
        asset=asset,
        request=request,
    )
    asset.save.assert_called_once_with(update_fields=["is_deleted", "deleted_at"])