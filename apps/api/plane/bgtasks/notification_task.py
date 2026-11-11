# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json
import uuid
from uuid import UUID


# Module imports
from plane.db.models import (
    IssueMention,
    IssueSubscriber,
    Project,
    User,
    IssueAssignee,
    Issue,
    State,
    EmailNotificationLog,
    Notification,
    IssueComment,
    IssueActivity,
    UserNotificationPreference,
    ProjectMember,
)
from django.db.models import Subquery

# Third Party imports
from celery import shared_task
from bs4 import BeautifulSoup


def update_mentions_for_issue(issue, project, new_mentions, removed_mention):
    aggregated_issue_mentions = []
    for mention_id in new_mentions:
        aggregated_issue_mentions.append(
            IssueMention(
                mention_id=mention_id,
                issue=issue,
                project=project,
                workspace_id=project.workspace_id,
            )
        )

    IssueMention.objects.bulk_create(aggregated_issue_mentions, batch_size=100)
    IssueMention.objects.filter(issue=issue, mention__in=removed_mention).delete()


# Adds mentions as subscribers
def extract_mentions_as_subscribers(project_id, issue_id, mentions):
    # mentions is an array of User IDs representing the FILTERED set of mentioned users

    bulk_mention_subscribers = []

    for mention_id in mentions:
        # If the particular mention has not already been subscribed to the issue, he must be sent the mentioned notification # noqa: E501
        if (
            not IssueSubscriber.objects.filter(
                issue_id=issue_id, subscriber_id=mention_id, project_id=project_id
            ).exists()
            and not IssueAssignee.objects.filter(
                project_id=project_id, issue_id=issue_id, assignee_id=mention_id
            ).exists()
            and not Issue.objects.filter(project_id=project_id, pk=issue_id, created_by_id=mention_id).exists()
            and ProjectMember.objects.filter(project_id=project_id, member_id=mention_id, is_active=True).exists()
        ):
            project = Project.objects.get(pk=project_id)

            bulk_mention_subscribers.append(
                IssueSubscriber(
                    workspace_id=project.workspace_id,
                    project_id=project_id,
                    issue_id=issue_id,
                    subscriber_id=mention_id,
                )
            )
    return bulk_mention_subscribers


def _extract_html_mentions(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
        mention_tags = soup.find_all("mention-component", attrs={"entity_name": "user_mention"})
        mentions = [tag["entity_identifier"] for tag in mention_tags]
        return list(set(mentions))
    except Exception:
        return []


def _extract_description_mentions(issue_instance):
    try:
        html = json.loads(issue_instance).get("description_html")
    except Exception:
        return []
    return _extract_html_mentions(html)


def _discover_description_mentions(requested_data, current_instance, project_members):
    previous_mentions = _extract_description_mentions(current_instance)
    current_mentions = _extract_description_mentions(requested_data)
    added_mentions = [mention for mention in current_mentions if mention not in previous_mentions]
    new_mentions = list(set(added_mentions) & {str(member) for member in project_members})
    removed_mentions = [mention for mention in previous_mentions if mention not in current_mentions]
    return current_mentions, new_mentions, removed_mentions


def _discover_comment_mentions(activities, project_members):
    active_member_ids = set(project_members)
    all_mentions = []
    new_mentions = []
    for activity in activities:
        if activity.get("issue_comment") is None:
            continue

        current_mentions = _extract_html_mentions(activity.get("new_value"))
        previous_value = activity.get("old_value")
        previous_mentions = _extract_html_mentions(previous_value) if previous_value is not None else []
        all_mentions.extend(current_mentions)
        new_mentions.extend(
            mention
            for mention in current_mentions
            if mention not in previous_mentions and UUID(mention) in active_member_ids
        )
    return all_mentions, new_mentions


def _issue_payload(issue, issue_id, *, include_project=False):
    payload = {
        "id": str(issue_id),
        "name": str(issue.name),
        "identifier": str(issue.project.identifier),
        "sequence_id": issue.sequence_id,
        "state_name": issue.state.name,
        "state_group": issue.state.group,
    }
    if include_project:
        payload["project_id"] = str(issue.project.id)
        payload["workspace_slug"] = str(issue.project.workspace.slug)
    return payload


def _activity_payload(activity):
    old_identifier = activity.get("old_identifier")
    new_identifier = activity.get("new_identifier")
    return {
        "id": str(activity.get("id")),
        "verb": str(activity.get("verb")),
        "field": str(activity.get("field")),
        "actor": str(activity.get("actor_id")),
        "new_value": str(activity.get("new_value")),
        "old_value": str(activity.get("old_value")),
        "old_identifier": str(old_identifier) if old_identifier else None,
        "new_identifier": str(new_identifier) if new_identifier else None,
    }


def _mention_notification(
    project, issue, issue_id, actor_id, mention_id, message, activity_data, *, include_project=False
):
    return Notification(
        workspace=project.workspace,
        sender="in_app:issue_activities:mentioned",
        triggered_by_id=actor_id,
        receiver_id=mention_id,
        entity_identifier=issue_id,
        entity_name="issue",
        project=project,
        message=message,
        data={
            "issue": _issue_payload(issue, issue_id, include_project=include_project),
            "issue_activity": activity_data,
        },
    )


def _mention_email_log(issue, issue_id, actor_id, mention_id, activity_data, activity_time, *, include_project=False):
    return EmailNotificationLog(
        triggered_by_id=actor_id,
        receiver_id=mention_id,
        entity_identifier=issue_id,
        entity_name="issue",
        data={
            "issue": _issue_payload(issue, issue_id, include_project=include_project),
            "issue_activity": {**activity_data, "field": "mention", "activity_time": activity_time},
        },
    )


def _build_comment_mention_notifications(project, issue, issue_id, actor_id, actor, mention_ids, activities):
    notifications = []
    email_logs = []
    for mention_id in mention_ids:
        if mention_id == actor_id:
            continue

        preference = UserNotificationPreference.objects.get(user_id=mention_id)
        for activity in activities:
            activity_data = _activity_payload(activity)
            message = f"{actor.display_name} has mentioned you in a comment in issue {issue.name}"
            notifications.append(
                _mention_notification(project, issue, issue_id, actor_id, mention_id, message, activity_data)
            )
            if preference.mention:
                email_logs.append(
                    _mention_email_log(
                        issue,
                        issue_id,
                        actor_id,
                        mention_id,
                        activity_data,
                        activity.get("created_at"),
                        include_project=True,
                    )
                )
    return notifications, email_logs


def _build_description_mention_notifications(
    project, issue, issue_id, actor_id, mention_ids, activities, last_activity
):
    use_saved_activity = (
        last_activity is not None and last_activity.field == "description" and actor_id == str(last_activity.actor_id)
    )
    if use_saved_activity:
        activities = (
            {
                "id": last_activity.id,
                "verb": last_activity.verb,
                "field": last_activity.field,
                "actor_id": last_activity.actor_id,
                "new_value": last_activity.new_value,
                "old_value": last_activity.old_value,
                "old_identifier": last_activity.old_identifier,
                "new_identifier": last_activity.new_identifier,
                "created_at": str(last_activity.created_at),
            },
        )

    notifications = []
    email_logs = []
    for mention_id in mention_ids:
        if mention_id == actor_id:
            continue

        preference = UserNotificationPreference.objects.get(user_id=mention_id)
        for activity in activities:
            activity_data = _activity_payload(activity)
            message = f"You have been mentioned in the issue {issue.name}"
            notifications.append(
                _mention_notification(
                    project,
                    issue,
                    issue_id,
                    actor_id,
                    mention_id,
                    message,
                    activity_data,
                    include_project=use_saved_activity,
                )
            )
            if preference.mention:
                email_logs.append(
                    _mention_email_log(issue, issue_id, actor_id, mention_id, activity_data, activity.get("created_at"))
                )
    return notifications, email_logs


def _subscriber_sender(issue, issue_assignees, recipient_id):
    if issue.created_by_id and issue.created_by_id == recipient_id:
        return "in_app:issue_activities:created"
    if recipient_id in issue_assignees and issue.created_by_id not in issue_assignees:
        return "in_app:issue_activities:assigned"
    return "in_app:issue_activities:subscribed"


def _should_email_subscriber(activity, preference, project_id):
    field = activity.get("field")
    if field == "state":
        if preference.state_change:
            return True
        if (
            preference.issue_completed
            and State.objects.filter(
                project_id=project_id, pk=activity.get("new_identifier"), group="completed"
            ).exists()
        ):
            return True
    if field == "comment" and preference.comment:
        return True
    return preference.property_change


def _build_subscriber_notifications(
    project, project_id, issue, issue_id, actor_id, issue_subscribers, issue_assignees, activities
):
    bulk_notifications = []
    bulk_email_logs = []
    for recipient_id in issue_subscribers:
        sender = _subscriber_sender(issue, issue_assignees, recipient_id)
        preference = UserNotificationPreference.objects.get(user_id=recipient_id)

        for issue_activity in activities:
            # Related work items can produce activities for a different issue.
            if issue_activity.get("issue_detail").get("id") != issue_id:
                continue

            # Do not send notification for description update
            if issue_activity.get("field") == "description":
                continue

            send_email = _should_email_subscriber(issue_activity, preference, project_id)

            issue_comment = (
                IssueComment.objects.filter(
                    id=issue_activity.get("issue_comment"),
                    issue_id=issue_id,
                    project_id=project_id,
                    workspace_id=project.workspace_id,
                ).first()
                if issue_activity.get("issue_comment")
                else None
            )

            activity_data = _activity_payload(issue_activity)
            activity_data["issue_comment"] = str(issue_comment.comment_stripped if issue_comment is not None else "")

            bulk_notifications.append(
                Notification(
                    workspace=project.workspace,
                    sender=sender,
                    triggered_by_id=actor_id,
                    receiver_id=recipient_id,
                    entity_identifier=issue_id,
                    entity_name="issue",
                    project=project,
                    title=issue_activity.get("comment"),
                    data={
                        "issue": _issue_payload(issue, issue_id),
                        "issue_activity": activity_data,
                    },
                )
            )
            if send_email:
                bulk_email_logs.append(
                    EmailNotificationLog(
                        triggered_by_id=actor_id,
                        receiver_id=recipient_id,
                        entity_identifier=issue_id,
                        entity_name="issue",
                        data={
                            "issue": _issue_payload(issue, issue_id, include_project=True),
                            "issue_activity": {
                                **activity_data,
                                "activity_time": issue_activity.get("created_at"),
                            },
                        },
                    )
                )
    return bulk_notifications, bulk_email_logs


@shared_task
def notifications(
    type,
    issue_id,
    project_id,
    actor_id,
    subscriber,
    issue_activities_created,
    requested_data,
    current_instance,
):
    try:
        issue_activities_created = (
            json.loads(issue_activities_created) if issue_activities_created is not None else None
        )
        if type in (
            "cycle.activity.created",
            "cycle.activity.deleted",
            "module.activity.created",
            "module.activity.deleted",
            "issue_reaction.activity.created",
            "issue_reaction.activity.deleted",
            "comment_reaction.activity.created",
            "comment_reaction.activity.deleted",
            "issue_vote.activity.created",
            "issue_vote.activity.deleted",
            "issue_draft.activity.created",
            "issue_draft.activity.updated",
            "issue_draft.activity.deleted",
        ):
            return

        project_members = ProjectMember.objects.filter(project_id=project_id, is_active=True).values_list(
            "member_id", flat=True
        )

        current_description_mentions, new_description_mentions, removed_description_mentions = (
            _discover_description_mentions(requested_data, current_instance, project_members)
        )
        description_mention_subscribers = extract_mentions_as_subscribers(
            project_id=project_id, issue_id=issue_id, mentions=current_description_mentions
        )

        all_comment_mentions, new_comment_mentions = _discover_comment_mentions(
            issue_activities_created, project_members
        )
        comment_mention_subscribers = extract_mentions_as_subscribers(
            project_id=project_id, issue_id=issue_id, mentions=all_comment_mentions
        )

        # Newly mentioned users receive mention notifications instead of ordinary subscriber notifications.
        issue_subscribers = list(
            IssueSubscriber.objects.filter(
                project_id=project_id,
                issue_id=issue_id,
                subscriber__in=Subquery(project_members),
            )
            .exclude(subscriber_id__in=[*new_description_mentions, *new_comment_mentions, actor_id])
            .values_list("subscriber", flat=True)
        )

        issue = Issue.objects.filter(pk=issue_id).first()

        if subscriber:
            # add the user to issue subscriber
            try:
                _ = IssueSubscriber.objects.get_or_create(
                    project_id=project_id, issue_id=issue_id, subscriber_id=actor_id
                )
            except Exception:
                pass

        project = Project.objects.get(pk=project_id)

        issue_assignees = IssueAssignee.objects.filter(
            issue_id=issue_id,
            project_id=project_id,
            assignee__in=Subquery(project_members),
        ).values_list("assignee", flat=True)

        issue_subscribers = list(set(issue_subscribers) - {uuid.UUID(actor_id)})

        bulk_notifications, bulk_email_logs = _build_subscriber_notifications(
            project=project,
            project_id=project_id,
            issue=issue,
            issue_id=issue_id,
            actor_id=actor_id,
            issue_subscribers=issue_subscribers,
            issue_assignees=issue_assignees,
            activities=issue_activities_created,
        )

        # Add Mentioned as Issue Subscribers
        IssueSubscriber.objects.bulk_create(
            description_mention_subscribers + comment_mention_subscribers,
            batch_size=100,
            ignore_conflicts=True,
        )

        last_activity = IssueActivity.objects.filter(issue_id=issue_id).order_by("-created_at").first()

        actor = User.objects.get(pk=actor_id)

        mention_notifications, mention_email_logs = _build_comment_mention_notifications(
            project=project,
            issue=issue,
            issue_id=issue_id,
            actor_id=actor_id,
            actor=actor,
            mention_ids=new_comment_mentions,
            activities=issue_activities_created,
        )
        bulk_notifications.extend(mention_notifications)
        bulk_email_logs.extend(mention_email_logs)

        mention_notifications, mention_email_logs = _build_description_mention_notifications(
            project=project,
            issue=issue,
            issue_id=issue_id,
            actor_id=actor_id,
            mention_ids=new_description_mentions,
            activities=issue_activities_created,
            last_activity=last_activity,
        )
        bulk_notifications.extend(mention_notifications)
        bulk_email_logs.extend(mention_email_logs)

        # save new mentions for the particular issue and remove the mentions that has been deleted from the description # noqa: E501
        update_mentions_for_issue(
            issue=issue,
            project=project,
            new_mentions=new_description_mentions,
            removed_mention=removed_description_mentions,
        )
        # Bulk create notifications
        Notification.objects.bulk_create(bulk_notifications, batch_size=100)
        EmailNotificationLog.objects.bulk_create(bulk_email_logs, batch_size=100, ignore_conflicts=True)
        return
    except Exception as e:
        print(e)
        return
