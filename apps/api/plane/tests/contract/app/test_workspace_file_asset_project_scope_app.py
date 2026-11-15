
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

from unittest import mock
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from django.db import IntegrityError

from plane.app.views.asset.v2 import (
    DuplicateAssetEndpoint,
    ProjectBulkAssetEndpoint,
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

# Helper methods
def user_asset_url(asset_id=None):
    base_url = "/api/assets/v2/user-assets/"
    return f"{base_url}{asset_id}/" if asset_id else base_url

def static_asset_url(asset_id):
    return f"/api/assets/v2/static/{asset_id}/"

def project_asset_url(slug, project_id, asset_id=None):
    base_url = f"/api/assets/v2/workspaces/{slug}/projects/{project_id}/"
    return f"{base_url}{asset_id}/" if asset_id else base_url


def restore_asset_url(slug, asset_id):
    return f"/api/assets/v2/workspaces/{slug}/restore/{asset_id}/"

def bulk_asset_url(slug, project_id, entity_id):
    return (
        f"/api/assets/v2/workspaces/{slug}/projects/"
        f"{project_id}/{entity_id}/bulk/"
    )

def asset_check_url(slug, asset_id):
    return f"/api/assets/v2/workspaces/{slug}/check/{asset_id}/"


def duplicate_asset_url(slug, asset_id):
    return f"/api/assets/v2/workspaces/{slug}/duplicate-assets/{asset_id}/"


def workspace_download_url(slug, asset_id):
    return f"/api/assets/v2/workspaces/{slug}/download/{asset_id}/"


def project_download_url(slug, project_id, asset_id):
    return (
        f"/api/assets/v2/workspaces/{slug}/projects/"
        f"{project_id}/download/{asset_id}/"
    )
    
# Tests start here
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

        assert response.status_code == status.HTTP_302_FOUND
        assert response.url == "https://signed.example/download"
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

        assert response.status_code == status.HTTP_302_FOUND
        mock_storage.return_value.generate_presigned_url.assert_called_once()

    @pytest.mark.django_db
    def test_patch_project_asset_allowed_for_project_member(
        self, session_client, workspace, project_asset
    ):
        """An active project member can update an uploaded asset."""
        url = detail_url(workspace.slug, project_asset.id)

        response = session_client.patch(
            url, {"attributes": {"name": "updated.pdf"}}, format="json"
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        project_asset.refresh_from_db()
        assert project_asset.attributes.get("name") == "updated.pdf"
        assert project_asset.is_uploaded is True

    @pytest.mark.django_db
    def test_delete_project_asset_allowed_for_project_member(
        self, session_client, workspace, project_asset
    ):
        """An active project member can delete a project asset."""
        url = detail_url(workspace.slug, project_asset.id)

        response = session_client.delete(url)

        assert response.status_code == status.HTTP_204_NO_CONTENT

        project_asset.refresh_from_db()
        assert project_asset.is_deleted is True
        assert project_asset.deleted_at is not None

    @pytest.mark.django_db
    def test_get_project_asset_not_uploaded_returns_not_found(
        self, session_client, workspace, project_asset
    ):
        """An asset that has not been uploaded returns 404."""
        project_asset.is_uploaded = False
        project_asset.save(update_fields=["is_uploaded"])

        url = detail_url(workspace.slug, project_asset.id)
        response = session_client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.django_db
    def test_patch_workspace_level_asset_allowed(
        self, outsider_client, workspace, workspace_logo_asset
    ):
        """A workspace member can update a workspace-level asset."""
        url = detail_url(workspace.slug, workspace_logo_asset.id)

        response = outsider_client.patch(
            url, {"attributes": {"name": "updated-logo.png"}}, format="json"
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.django_db
    def test_delete_workspace_level_asset_allowed(
        self, outsider_client, workspace, workspace_logo_asset
    ):
        """A workspace member can delete a workspace-level asset."""
        url = detail_url(workspace.slug, workspace_logo_asset.id)

        response = outsider_client.delete(url)

        assert response.status_code == status.HTTP_204_NO_CONTENT

    @pytest.mark.django_db
    def test_post_missing_entity_type(self, session_client):
        response = session_client.post(
            user_asset_url(),
            {
                "name": "avatar.png",
                "type": "image/png",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_post_valid_cover_asset(self, session_client, create_user):
        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_post.return_value = {
                "url": "https://example.com/upload"
            }

            response = session_client.post(
                user_asset_url(),
                {
                    "name": "cover.png",
                    "type": "image/png",
                    "size": 2048,
                    "entity_type": "USER_COVER",
                },
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK

        asset = FileAsset.objects.get(id=response.data["asset_id"])
        assert asset.user_id == create_user.id
        assert asset.entity_type == FileAsset.EntityTypeContext.USER_COVER

    @pytest.mark.django_db
    def test_patch_avatar_with_missing_storage_metadata(
        self, session_client, create_user
    ):
        asset = FileAsset.objects.create(
            attributes={"name": "avatar.png", "type": "image/png", "size": 1024},
            asset="avatar.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            storage_metadata={},
        )

        with (
            mock.patch("plane.app.views.asset.v2.get_asset_object_metadata.delay") as metadata_task,
            mock.patch("plane.app.views.asset.v2.invalidate_cache_directly"),
        ):
            response = session_client.patch(
                user_asset_url(asset.id),
                {"attributes": {"name": "updated.png"}},
                format="json",
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        metadata_task.assert_called_once_with(asset_id=str(asset.id))

        asset.refresh_from_db()
        assert asset.is_uploaded is True
        assert asset.attributes["name"] == "updated.png"

    @pytest.mark.django_db
    def test_patch_avatar_replaces_previous_asset(
        self, session_client, create_user
    ):
        previous_asset = FileAsset.objects.create(
            attributes={"name": "old-avatar.png", "type": "image/png", "size": 512},
            asset="old-avatar.png",
            size=512,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            is_uploaded=True,
            storage_metadata={"size": 512},
        )

        new_asset = FileAsset.objects.create(
            attributes={"name": "new-avatar.png", "type": "image/png", "size": 1024},
            asset="new-avatar.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            storage_metadata={"size": 1024},
        )

        create_user.avatar_asset_id = previous_asset.id
        create_user.save()

        with mock.patch("plane.app.views.asset.v2.invalidate_cache_directly"):
            response = session_client.patch(
                user_asset_url(new_asset.id),
                {},
                format="json",
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        create_user.refresh_from_db()
        previous_asset.refresh_from_db()
        new_asset.refresh_from_db()

        assert create_user.avatar_asset_id == new_asset.id
        assert previous_asset.is_deleted is True
        assert previous_asset.deleted_at is not None
        assert new_asset.is_uploaded is True

    @pytest.mark.django_db
    def test_patch_user_cover_asset(self, session_client, create_user):
        asset = FileAsset.objects.create(
            attributes={"name": "cover.png", "type": "image/png", "size": 1024},
            asset="cover.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_COVER,
            is_uploaded=False,
            storage_metadata={"size": 1024},
        )

        with mock.patch("plane.app.views.asset.v2.invalidate_cache_directly"):
            response = session_client.patch(
                user_asset_url(asset.id),
                {},
                format="json",
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        asset.refresh_from_db()
        create_user.refresh_from_db()

        assert asset.is_uploaded is True
        assert create_user.cover_image_asset_id == asset.id

    @pytest.mark.django_db
    def test_delete_user_cover_asset(self, session_client, create_user):
        asset = FileAsset.objects.create(
            attributes={"name": "cover.png", "type": "image/png", "size": 1024},
            asset="cover.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_COVER,
            is_uploaded=True,
            storage_metadata={"size": 1024},
        )

        create_user.cover_image_asset_id = asset.id
        create_user.save()

        with mock.patch("plane.app.views.asset.v2.invalidate_cache_directly"):
            response = session_client.delete(user_asset_url(asset.id))

        assert response.status_code == status.HTTP_204_NO_CONTENT

        asset.refresh_from_db()
        create_user.refresh_from_db()

        assert asset.is_deleted is True
        assert asset.deleted_at is not None
        assert create_user.cover_image_asset_id is None

    @pytest.mark.django_db
    def test_patch_preserves_attributes_when_not_provided(
        self, session_client, create_user
    ):
        original_attributes = {
            "name": "avatar.png",
            "type": "image/png",
            "size": 1024,
        }

        asset = FileAsset.objects.create(
            attributes=original_attributes,
            asset="avatar-preserve.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            storage_metadata={"size": 1024},
        )

        with mock.patch("plane.app.views.asset.v2.invalidate_cache_directly"):
            response = session_client.patch(
                user_asset_url(asset.id),
                {},
                format="json",
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        asset.refresh_from_db()
        assert asset.attributes == original_attributes

    @pytest.mark.django_db
    def test_delete_user_asset_does_not_delete_unrelated_entity(
        self, session_client, create_user
    ):
        asset = FileAsset.objects.create(
            attributes={"name": "avatar.png", "type": "image/png", "size": 1024},
            asset="avatar-unrelated.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            is_uploaded=True,
            storage_metadata={"size": 1024},
        )

        with mock.patch("plane.app.views.asset.v2.invalidate_cache_directly"):
            response = session_client.delete(user_asset_url(asset.id))

        assert response.status_code == status.HTTP_204_NO_CONTENT

        asset.refresh_from_db()
        assert asset.is_deleted is True

    @pytest.mark.django_db
    def test_workspace_upload_rejects_workspace_logo_for_non_admin(
        self, outsider_client, workspace
    ):
        response = outsider_client.post(
            f"/api/assets/v2/workspaces/{workspace.slug}/",
            {
                "name": "logo.png",
                "type": "image/png",
                "size": 512,
                "entity_type": FileAsset.EntityTypeContext.WORKSPACE_LOGO,
                "entity_identifier": str(workspace.id),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["error"] == (
            "Only workspace admins can upload a workspace logo."
        )

    @pytest.mark.django_db
    def test_workspace_upload_creates_asset_and_returns_presigned_data(
        self, session_client, workspace, project
    ):
        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_post.return_value = {
                "url": "https://example.com/upload"
            }

            response = session_client.post(
                f"/api/assets/v2/workspaces/{workspace.slug}/",
                {
                    "name": "cover.png",
                    "type": "image/png",
                    "size": 2048,
                    "entity_type": FileAsset.EntityTypeContext.PROJECT_COVER,
                    "entity_identifier": str(project.id),
                },
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK

        asset = FileAsset.objects.get(id=response.data["asset_id"])
        assert asset.workspace_id == workspace.id
        assert asset.project_id == project.id
        assert asset.entity_type == FileAsset.EntityTypeContext.PROJECT_COVER
        assert asset.attributes["name"] == "cover.png"
        assert response.data["upload_data"] == {
            "url": "https://example.com/upload"
        }
        mock_storage.return_value.generate_presigned_post.assert_called_once()

    @pytest.mark.django_db
    def test_patch_project_asset_queues_metadata_and_updates_attributes(
        self, session_client, workspace, project_asset
    ):
        project_asset.storage_metadata = {}
        project_asset.save(update_fields=["storage_metadata"])

        with mock.patch(
            "plane.app.views.asset.v2.get_asset_object_metadata.delay"
        ) as metadata_task:
            response = session_client.patch(
                detail_url(workspace.slug, project_asset.id),
                {"attributes": {"name": "updated-attachment.pdf"}},
                format="json",
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT
        metadata_task.assert_called_once_with(
            asset_id=str(project_asset.id)
        )

        project_asset.refresh_from_db()
        assert project_asset.is_uploaded is True
        assert project_asset.attributes == {
            "name": "updated-attachment.pdf"
        }
        
@pytest.mark.contract
class TestStaticFileAssetEndpoint:

    @pytest.mark.django_db
    def test_get_unuploaded_asset_returns_404(self, session_client, create_user):
        asset = FileAsset.objects.create(
            attributes={"name": "avatar.png", "type": "image/png"},
            asset="avatar.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            is_uploaded=False,
        )

        response = session_client.get(static_asset_url(asset.id))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.django_db
    def test_get_unsupported_entity_type_returns_400(
        self, session_client, project_asset
    ):
        project_asset.is_uploaded = True
        project_asset.save()

        response = session_client.get(static_asset_url(project_asset.id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_get_image_asset_redirects_inline(self, session_client, create_user):
        asset = FileAsset.objects.create(
            attributes={"name": "avatar.png", "type": "image/png"},
            asset="avatar.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            is_uploaded=True,
        )

        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://example.com/avatar.png"
            )

            response = session_client.get(static_asset_url(asset.id))

        assert response.status_code == 302
        assert response.url == "https://example.com/avatar.png"

        mock_storage.return_value.generate_presigned_url.assert_called_once_with(
            object_name=asset.asset.name,
            disposition="inline",
        )

    @pytest.mark.django_db
    def test_get_script_asset_redirects_as_attachment(
        self, session_client, create_user
    ):
        script_type = "application/javascript"

        asset = FileAsset.objects.create(
            attributes={"name": "script.js", "type": script_type},
            asset="script.js",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
            is_uploaded=True,
        )

        with (
            mock.patch(
                "plane.app.views.asset.v2.settings.SCRIPT_CAPABLE_MIME_TYPES",
                [script_type],
            ),
            mock.patch(S3_STORAGE_PATH) as mock_storage,
        ):
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://example.com/script.js"
            )

            response = session_client.get(static_asset_url(asset.id))

        assert response.status_code == 302
        mock_storage.return_value.generate_presigned_url.assert_called_once_with(
            object_name=asset.asset.name,
            disposition="attachment",
        )

    @pytest.mark.django_db
    def test_get_cover_asset_redirects_inline(self, session_client, create_user):
        asset = FileAsset.objects.create(
            attributes={"name": "cover.png", "type": "image/png"},
            asset="cover.png",
            size=1024,
            user=create_user,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.USER_COVER,
            is_uploaded=True,
        )

        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://example.com/cover.png"
            )

            response = session_client.get(static_asset_url(asset.id))

        assert response.status_code == 302
        mock_storage.return_value.generate_presigned_url.assert_called_once_with(
            object_name=asset.asset.name,
            disposition="inline",
        )
        
@pytest.mark.contract
class TestProjectAssetEndpoint:

    @pytest.mark.django_db
    def test_post_invalid_entity_type(self, session_client, workspace, project):
        response = session_client.post(
            project_asset_url(workspace.slug, project.id),
            {
                "name": "test.png",
                "type": "image/png",
                "entity_type": "INVALID_ENTITY",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_post_invalid_file_type(self, session_client, workspace, project):
        response = session_client.post(
            project_asset_url(workspace.slug, project.id),
            {
                "name": "test.pdf",
                "type": "application/pdf",
                "entity_type": "ISSUE_ATTACHMENT",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_post_valid_asset(self, session_client, workspace, project):
        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_post.return_value = {
                "url": "https://example.com/upload"
            }

            response = session_client.post(
                project_asset_url(workspace.slug, project.id),
                {
                    "name": "attachment.png",
                    "type": "image/png",
                    "size": 2048,
                    "entity_type": "ISSUE_ATTACHMENT",
                },
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK

        asset = FileAsset.objects.get(id=response.data["asset_id"])
        assert asset.workspace_id == workspace.id
        assert asset.project_id == project.id
        assert asset.entity_type == FileAsset.EntityTypeContext.ISSUE_ATTACHMENT
        assert asset.attributes["name"] == "attachment.png"

        mock_storage.return_value.generate_presigned_post.assert_called_once()

    @pytest.mark.django_db
    def test_patch_asset_updates_attributes(
        self, session_client, workspace, project_asset
    ):
        url = project_asset_url(
            workspace.slug, project_asset.project_id, project_asset.id
        )

        response = session_client.patch(
            url,
            {"attributes": {"name": "updated.png"}},
            format="json",
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        project_asset.refresh_from_db()
        assert project_asset.is_uploaded is True
        assert project_asset.attributes == {"name": "updated.png"}

    @pytest.mark.django_db
    def test_patch_asset_triggers_metadata_task(
        self, session_client, workspace, project_asset
    ):
        project_asset.storage_metadata = {}
        project_asset.save(update_fields=["storage_metadata"])

        url = project_asset_url(
            workspace.slug, project_asset.project_id, project_asset.id
        )

        with mock.patch(
            "plane.app.views.asset.v2.get_asset_object_metadata.delay"
        ) as metadata_task:
            response = session_client.patch(url, {}, format="json")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        metadata_task.assert_called_once_with(
            asset_id=str(project_asset.id)
        )

    @pytest.mark.django_db
    def test_delete_asset_marks_it_deleted(
        self, session_client, workspace, project_asset
    ):
        url = project_asset_url(
            workspace.slug, project_asset.project_id, project_asset.id
        )

        response = session_client.delete(url)

        assert response.status_code == status.HTTP_204_NO_CONTENT

        project_asset.refresh_from_db()
        assert project_asset.is_deleted is True
        assert project_asset.deleted_at is not None

    @pytest.mark.django_db
    def test_get_unuploaded_asset_returns_404(
        self, session_client, workspace, project_asset
    ):
        project_asset.is_uploaded = False
        project_asset.save(update_fields=["is_uploaded"])

        url = project_asset_url(
            workspace.slug, project_asset.project_id, project_asset.id
        )

        response = session_client.get(url)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.django_db
    def test_get_uploaded_asset_redirects_to_signed_url(
        self, session_client, workspace, project_asset
    ):
        url = project_asset_url(
            workspace.slug, project_asset.project_id, project_asset.id
        )

        with mock.patch(S3_STORAGE_PATH) as mock_storage:
            mock_storage.return_value.generate_presigned_url.return_value = (
                "https://example.com/download"
            )

            response = session_client.get(url)

        assert response.status_code == status.HTTP_302_FOUND
        assert response.url == "https://example.com/download"

        mock_storage.return_value.generate_presigned_url.assert_called_once_with(
            object_name=project_asset.asset.name,
            disposition="attachment",
            filename=project_asset.attributes.get("name"),
        )


@pytest.mark.contract
class TestAssetRestoreEndpoint:

    @pytest.mark.django_db
    def test_restore_deleted_asset(self, session_client, workspace, project_asset):
        project_asset.is_deleted = True
        project_asset.deleted_at = timezone.now()
        project_asset.save(update_fields=["is_deleted", "deleted_at"])

        response = session_client.post(
            restore_asset_url(workspace.slug, project_asset.id)
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        project_asset.refresh_from_db()
        assert project_asset.is_deleted is False
        assert project_asset.deleted_at is None
        
 
@pytest.mark.contract
class TestProjectBulkAssetEndpoint:

    @pytest.mark.django_db
    def test_bulk_update_missing_asset_ids(self, session_client, workspace, project):
        response = session_client.post(
            bulk_asset_url(workspace.slug, project.id, uuid4()),
            {"asset_ids": []},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_bulk_update_nonexistent_asset(self, session_client, workspace, project):
        response = session_client.post(
            bulk_asset_url(workspace.slug, project.id, uuid4()),
            {"asset_ids": [str(uuid4())]},
            format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.django_db
    def test_upload_rejects_invalid_entity_type(self, session_client, workspace):
        response = session_client.post(
            f"/api/assets/v2/workspaces/{workspace.slug}/",
            {
                "name": "test.png",
                "type": "image/png",
                "size": 512,
                "entity_type": "INVALID_ENTITY",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error"] == "Invalid entity type."

    @pytest.mark.django_db
    def test_upload_rejects_invalid_file_type(self, session_client, workspace):
        response = session_client.post(
            f"/api/assets/v2/workspaces/{workspace.slug}/",
            {
                "name": "test.exe",
                "type": "application/octet-stream",
                "size": 512,
                "entity_type": FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid file type" in response.data["error"]

@pytest.mark.contract
class TestWorkspaceAssetEntityManagement:
    @pytest.mark.django_db
    def test_get_entity_id_field_returns_workspace_id(self):

        endpoint = WorkspaceFileAssetEndpoint()
        result = endpoint.get_entity_id_field(
            FileAsset.EntityTypeContext.WORKSPACE_LOGO, "workspace-123"
        )

        assert result == {"workspace_id": "workspace-123"}
        
    @pytest.mark.django_db
    def test_get_entity_id_field_returns_project_id(self):

        endpoint = WorkspaceFileAssetEndpoint()
        result = endpoint.get_entity_id_field(
            FileAsset.EntityTypeContext.PROJECT_COVER, "project-123"
        )

        assert result == {"project_id": "project-123"}
        
    @pytest.mark.django_db
    def test_get_entity_id_field_returns_empty_for_unsupported_type(self):

        endpoint = WorkspaceFileAssetEndpoint()
        result = endpoint.get_entity_id_field("UNSUPPORTED", "entity-123")


        assert result == {}
    @pytest.mark.django_db
    def test_asset_delete_marks_existing_asset_deleted(self, project_asset):

        endpoint = WorkspaceFileAssetEndpoint()
        endpoint.asset_delete(project_asset.id)

        project_asset.refresh_from_db()
        assert project_asset.is_deleted is True
        assert project_asset.deleted_at is not None
        
    @pytest.mark.django_db
    def test_update_entity_asset_replaces_previous_asset(
        self, project, project_asset, create_user
    ):

        previous_asset = FileAsset.objects.create(
            attributes={"name": "old.png", "type": "image/png", "size": 100},
            asset=f"{project.workspace_id}/{uuid4().hex}-old.png",
            size=100,
            workspace=project.workspace,
            created_by=create_user,
            entity_type=FileAsset.EntityTypeContext.PROJECT_COVER,
            project=project,
        )
        project.cover_image_asset_id = previous_asset.id
        project.save()

        endpoint = WorkspaceFileAssetEndpoint()
        endpoint.update_entity_asset(
            project,
            project_asset.id,
            "cover_image_asset_id",
            "cover_image",
        )

        project.refresh_from_db()
        previous_asset.refresh_from_db()

        assert str(project.cover_image_asset_id) == str(project_asset.id)
        assert project.cover_image == ""
        assert previous_asset.is_deleted is True
        
    
    @pytest.mark.django_db
    def test_entity_asset_delete_clears_project_cover(
        self, project_asset, project
    ):

        endpoint = WorkspaceFileAssetEndpoint()

        endpoint.entity_asset_delete(
            FileAsset.EntityTypeContext.PROJECT_COVER,
            project_asset,
            mock.Mock(),
        )

        project.refresh_from_db()
        assert project.cover_image_asset_id is None  
        
    @pytest.mark.django_db
    def test_entity_asset_save_unsupported_entity_type(self, workspace_logo_asset):
        endpoint = WorkspaceFileAssetEndpoint()
        request = mock.Mock()

        with mock.patch.object(endpoint, "update_entity_asset") as update_asset, \
             mock.patch.object(endpoint, "invalidate_workspace_asset_cache") as invalidate_cache:

            endpoint.entity_asset_save(
                workspace_logo_asset.id,
                None,
                workspace_logo_asset,
                request,
            )

        update_asset.assert_not_called()
        invalidate_cache.assert_not_called()
        
    @pytest.mark.django_db        
    def test_entity_asset_save_workspace_logo_updates_workspace_and_invalidates_cache(
        self, db, workspace_logo_asset, workspace
    ):
        endpoint = WorkspaceFileAssetEndpoint()
        request = mock.Mock()

        with mock.patch(
            "plane.app.views.asset.v2.invalidate_cache_directly"
        ) as mock_invalidate:
            endpoint.entity_asset_save(
                workspace_logo_asset.id,
                FileAsset.EntityTypeContext.WORKSPACE_LOGO,
                workspace_logo_asset,
                request,
            )

        workspace.refresh_from_db()

        assert workspace.logo_asset_id == workspace_logo_asset.id
        assert workspace.logo == ""
        assert mock_invalidate.call_count == 3
        
    @pytest.mark.django_db
    def test_entity_asset_delete_workspace_logo_clears_reference_and_invalidates_cache(
        self, db, workspace_logo_asset, workspace
    ):
        endpoint = WorkspaceFileAssetEndpoint()
        request = mock.Mock()

        workspace.logo_asset_id = workspace_logo_asset.id
        workspace.save()

        with mock.patch(
            "plane.app.views.asset.v2.invalidate_cache_directly"
        ) as mock_invalidate:
            endpoint.entity_asset_delete(
                FileAsset.EntityTypeContext.WORKSPACE_LOGO,
                workspace_logo_asset,
                request,
            )

        workspace.refresh_from_db()

        assert workspace.logo_asset_id is None
        assert mock_invalidate.call_count == 3
        
    @pytest.mark.django_db
    def test_entity_asset_save_does_nothing_when_workspace_does_not_exist(self, db):
        endpoint = WorkspaceFileAssetEndpoint()
        request = mock.Mock()
        asset = mock.Mock(workspace_id=uuid4())

        with mock.patch.object(
            endpoint, "update_entity_asset"
        ) as mock_update:
            with mock.patch.object(
                endpoint, "invalidate_workspace_asset_cache"
            ) as mock_invalidate:
                endpoint.entity_asset_save(
                    uuid4(),
                    FileAsset.EntityTypeContext.WORKSPACE_LOGO,
                    asset,
                    request,
                )

        mock_update.assert_not_called()
        mock_invalidate.assert_not_called()
        
    @pytest.mark.django_db
    def test_entity_asset_delete_does_nothing_when_project_does_not_exist(self, db):
        endpoint = WorkspaceFileAssetEndpoint()
        request = mock.Mock()
        asset = mock.Mock(project_id=uuid4())

        endpoint.entity_asset_delete(
            FileAsset.EntityTypeContext.PROJECT_COVER,
            asset,
            request,
        )
        
    @pytest.mark.django_db
    def test_asset_delete_does_nothing_when_asset_does_not_exist(self, db):
        endpoint = WorkspaceFileAssetEndpoint()

        endpoint.asset_delete(uuid4())
     
@pytest.mark.django_db    
class TestProjectBulkAssetHelpers:
    
    @pytest.mark.django_db
    def setup_method(self):
        self.endpoint = ProjectBulkAssetEndpoint()

    @pytest.mark.django_db
    def test_update_assets_updates_records(self):
        assets = mock.Mock()
        update_fields = {"project_id": "project-123"}

        self.endpoint.update_assets(assets, update_fields)

        assets.update.assert_called_once_with(**update_fields)

    @pytest.mark.django_db
    def test_update_assets_ignores_integrity_error(self):
        assets = mock.Mock()
        assets.update.side_effect = IntegrityError

        self.endpoint.update_assets(
            assets,
            {"issue_id": "issue-123"},
            ignore_integrity_error=True,
        )

        assets.update.assert_called_once_with(issue_id="issue-123")

    @pytest.mark.django_db
    def test_update_assets_raises_integrity_error_when_not_ignored(self):
        assets = mock.Mock()
        assets.update.side_effect = IntegrityError

        with pytest.raises(IntegrityError):
            self.endpoint.update_assets(
                assets,
                {"issue_id": "issue-123"},
            )

    @pytest.mark.parametrize(
        "entity_type, expected_fields",
        [
            (
                FileAsset.EntityTypeContext.ISSUE_DESCRIPTION,
                {"issue_id": "entity-123", "project_id": "project-123"},
            ),
            (
                FileAsset.EntityTypeContext.COMMENT_DESCRIPTION,
                {"comment_id": "entity-123"},
            ),
            (
                FileAsset.EntityTypeContext.PAGE_DESCRIPTION,
                {"page_id": "entity-123"},
            ),
            (
                FileAsset.EntityTypeContext.DRAFT_ISSUE_DESCRIPTION,
                {"draft_issue_id": "entity-123"},
            ),
        ],
    )
    
    @pytest.mark.django_db
    def test_update_asset_entity_updates_relationship(
        self, entity_type, expected_fields
    ):
        assets = mock.Mock()

        self.endpoint.update_asset_entity(
            assets,
            entity_type,
            "entity-123",
            "project-123",
        )

        assets.update.assert_called_once_with(**expected_fields)

    @pytest.mark.django_db
    def test_update_asset_entity_ignores_unsupported_entity_type(self):
        assets = mock.Mock()

        self.endpoint.update_asset_entity(
            assets,
            FileAsset.EntityTypeContext.USER_AVATAR,
            "entity-123",
            "project-123",
        )

        assets.update.assert_not_called()

    @pytest.mark.django_db
    def test_update_asset_entity_project_cover(self):
        asset = mock.Mock()
        project = mock.Mock()
        assets = mock.Mock()
        assets.__iter__ = mock.Mock(return_value=iter([asset]))

        with mock.patch(
            "plane.app.views.asset.v2.Project.objects.get",
            return_value=project,
        ):
            with mock.patch.object(
                self.endpoint, "save_project_cover"
            ) as save_cover:
                self.endpoint.update_asset_entity(
                    assets,
                    FileAsset.EntityTypeContext.PROJECT_COVER,
                    None,
                    "project-123",
                )

        assets.update.assert_called_once_with(project_id="project-123")
        save_cover.assert_called_once_with(asset, project)
        