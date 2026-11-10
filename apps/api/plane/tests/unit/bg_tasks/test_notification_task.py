import json

import pytest
from django.core.serializers.json import DjangoJSONEncoder

from plane.app.serializers import IssueActivitySerializer
from plane.bgtasks.notification_task import notifications
from plane.db.models import (
    EmailNotificationLog,
    Issue,
    IssueActivity,
    IssueComment,
    IssueMention,
    IssueSubscriber,
    Notification,
    Project,
    ProjectMember,
    State,
    User,
    UserNotificationPreference,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


@pytest.fixture
def issue(workspace, create_user):
    project = Project.objects.create(name="Notifications", identifier="NOTIFY", workspace=workspace)
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    state = State.objects.create(name="Backlog", color="#000000", group="backlog", project=project, default=True)
    issue = Issue(name="Work item", project=project, state=state)
    issue.save(created_by_id=create_user.id)
    return issue


def _add_member(issue, name, *, active=True):
    user = User.objects.create(username=name, email=f"{name}@plane.so", first_name=name)
    ProjectMember.objects.create(project=issue.project, member=user, role=15, is_active=active)
    return user


def _subscribe(issue, user):
    IssueSubscriber.objects.create(project=issue.project, issue=issue, subscriber=user)


def _set_email_preferences(user, **preferences):
    defaults = dict(property_change=False, state_change=False, comment=False, mention=False, issue_completed=False)
    UserNotificationPreference.objects.filter(user=user).update(**(defaults | preferences))


def _mention(user):
    return f'<mention-component entity_name="user_mention" entity_identifier="{user.id}"></mention-component>'


def _record_activity(issue, field, **values):
    return IssueActivity.objects.create(
        project=issue.project, issue=issue, actor=issue.created_by, field=field, verb="updated", **values
    )


def _run_notifications(
    issue, activities, *, old_description="", new_description="", event_type="issue.activity.updated"
):
    notifications.run(
        type=event_type,
        issue_id=str(issue.id),
        project_id=str(issue.project_id),
        actor_id=str(issue.created_by_id),
        subscriber=False,
        issue_activities_created=json.dumps(IssueActivitySerializer(activities, many=True).data, cls=DjangoJSONEncoder),
        requested_data=json.dumps({"description_html": new_description}),
        current_instance=json.dumps({"description_html": old_description}),
    )


def test_ordinary_activity_notifies_only_active_non_actor_subscribers(issue):
    subscriber = _add_member(issue, "subscriber")
    inactive = _add_member(issue, "inactive", active=False)
    _subscribe(issue, subscriber)
    _subscribe(issue, inactive)
    _subscribe(issue, issue.created_by)
    activity = _record_activity(issue, "name", comment="updated the name to", old_value="Old", new_value=issue.name)

    _run_notifications(issue, [activity])

    notification = Notification.objects.get(entity_identifier=issue.id)
    assert notification.receiver_id == subscriber.id
    assert notification.triggered_by_id == issue.created_by_id
    assert notification.sender == "in_app:issue_activities:subscribed"
    assert notification.title == activity.comment
    assert notification.data["issue"]["id"] == str(issue.id)
    assert notification.data["issue_activity"]["id"] == str(activity.id)
    assert EmailNotificationLog.objects.get(entity_identifier=issue.id).receiver_id == subscriber.id


@pytest.mark.parametrize(
    ("field", "state_group", "enabled_preference", "send_email"),
    [
        pytest.param("comment", None, "comment", True, id="comment"),
        pytest.param("state", "started", "state_change", True, id="state-change"),
        pytest.param("state", "completed", "issue_completed", True, id="completion"),
        pytest.param("state", "started", "issue_completed", False, id="non-completion"),
        pytest.param("comment", None, "property_change", True, id="property-change-fallback"),
        pytest.param("name", None, None, False, id="email-disabled"),
    ],
)
def test_subscriber_email_preferences(issue, field, state_group, enabled_preference, send_email):
    subscriber = _add_member(issue, "subscriber")
    _subscribe(issue, subscriber)
    _set_email_preferences(subscriber, **({enabled_preference: True} if enabled_preference else {}))

    values = {}
    if state_group:
        state = State.objects.create(name=state_group, color="#000000", group=state_group, project=issue.project)
        values["new_identifier"] = state.id
    if field == "comment":
        comment = IssueComment.objects.create(
            project=issue.project, issue=issue, actor=issue.created_by, comment_html="<p>Comment text</p>"
        )
        values["issue_comment"] = comment
    activity = _record_activity(issue, field, **values)

    _run_notifications(issue, [activity])

    assert Notification.objects.get(entity_identifier=issue.id).receiver_id == subscriber.id
    email_logs = EmailNotificationLog.objects.filter(entity_identifier=issue.id)
    if send_email:
        email_log = email_logs.get()
        assert email_log.receiver_id == subscriber.id
        assert email_log.data["issue_activity"]["field"] == field
        assert email_log.data["issue_activity"]["activity_time"]
        if field == "comment":
            assert email_log.data["issue_activity"]["issue_comment"] == "Comment text"
    else:
        assert not email_logs.exists()


@pytest.mark.parametrize(
    ("previously_mentioned", "currently_mentioned"),
    [
        pytest.param(False, True, id="added"),
        pytest.param(True, True, id="retained"),
        pytest.param(True, False, id="removed"),
    ],
)
def test_description_mention_lifecycle(issue, previously_mentioned, currently_mentioned):
    user = _add_member(issue, "mentioned")
    _set_email_preferences(user)
    mention = _mention(user)
    if previously_mentioned:
        IssueMention.objects.create(project=issue.project, issue=issue, mention=user)
        _subscribe(issue, user)
    old_html = f"<p>Before {mention if previously_mentioned else ''}</p>"
    new_html = f"<p>After {mention if currently_mentioned else ''}</p>"
    activity = _record_activity(issue, "description", old_value=old_html, new_value=new_html)

    _run_notifications(issue, [activity], old_description=old_html, new_description=new_html)

    assert IssueMention.objects.filter(issue=issue, mention=user).exists() is currently_mentioned
    assert IssueSubscriber.objects.filter(issue=issue, subscriber=user).exists()
    in_app = Notification.objects.filter(entity_identifier=issue.id)
    if currently_mentioned and not previously_mentioned:
        notification = in_app.get()
        assert notification.receiver_id == user.id
        assert notification.sender == "in_app:issue_activities:mentioned"
    else:
        assert not in_app.exists()
    assert not EmailNotificationLog.objects.filter(entity_identifier=issue.id).exists()


@pytest.mark.parametrize("already_subscribed", [True, False], ids=["subscribed", "new-subscriber"])
def test_comment_edit_notifies_new_mention_and_preserves_ordinary_subscriber_delivery(issue, already_subscribed):
    existing = _add_member(issue, "existing")
    added = _add_member(issue, "added")
    _subscribe(issue, existing)
    if already_subscribed:
        _subscribe(issue, added)
    old_html = _mention(existing)
    new_html = old_html + _mention(added)
    comment = IssueComment.objects.create(
        project=issue.project, issue=issue, actor=issue.created_by, comment_html=new_html
    )
    activity = _record_activity(issue, "comment", issue_comment=comment, old_value=old_html, new_value=new_html)

    _run_notifications(issue, [activity], event_type="comment.activity.updated")

    assert IssueSubscriber.objects.filter(issue=issue, subscriber=added).exists()
    in_app = Notification.objects.filter(entity_identifier=issue.id).values_list("receiver_id", "sender")
    assert sorted(in_app) == sorted(
        [(existing.id, "in_app:issue_activities:subscribed"), (added.id, "in_app:issue_activities:mentioned")]
    )
    email_logs = EmailNotificationLog.objects.filter(entity_identifier=issue.id)
    assert sorted((log.receiver_id, log.data["issue_activity"]["field"]) for log in email_logs) == sorted(
        [(existing.id, "comment"), (added.id, "mention")]
    )


def test_ignored_event_creates_no_notifications(issue):
    subscriber = _add_member(issue, "subscriber")
    _subscribe(issue, subscriber)
    activity = _record_activity(issue, "cycle")

    _run_notifications(issue, [activity], event_type="cycle.activity.created")

    assert not Notification.objects.filter(entity_identifier=issue.id).exists()
    assert not EmailNotificationLog.objects.filter(entity_identifier=issue.id).exists()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Description mention email uses the subscriber ID")
@pytest.mark.parametrize("latest_field", ["description", "name"])
def test_description_mention_email_goes_to_mentioned_user(issue, latest_field):
    subscriber = _add_member(issue, "subscriber")
    mentioned = _add_member(issue, "mentioned")
    _subscribe(issue, subscriber)
    _set_email_preferences(subscriber)
    _set_email_preferences(mentioned, mention=True)
    activity = _record_activity(issue, latest_field)

    _run_notifications(issue, [activity], new_description=_mention(mentioned))

    email = EmailNotificationLog.objects.get(entity_identifier=issue.id)
    assert email.data["issue_activity"]["field"] == "mention"
    assert email.receiver_id == mentioned.id


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="Reused description activity reads an unset loop variable"
)
def test_description_mention_notifies_when_activity_is_reused(issue):
    mentioned = _add_member(issue, "mentioned")
    _set_email_preferences(mentioned)
    activity = _record_activity(issue, "description")

    _run_notifications(issue, [], new_description=_mention(mentioned))

    in_app = Notification.objects.filter(entity_identifier=issue.id, receiver=mentioned)
    assert [row.data["issue_activity"]["id"] for row in in_app] == [str(activity.id)]
