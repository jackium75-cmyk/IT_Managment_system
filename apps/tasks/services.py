"""
Business operations on tasks. Views call these functions; they never change
task state directly. Each operation runs in one transaction and writes the
task timeline, the activity diary and the audit log together.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.roles import Role, has_role, is_it_staff
from apps.core import audit
from apps.core.models import AuditLog, Sequence
from apps.diary.models import ActivityLog
from apps.diary import timekeeping
from apps.diary.timecalc import format_minutes

from .models import Task, TaskComment, TaskScheduleChange, TaskTimeEntry

LONG_TIMER_WARNING_MINUTES = 12 * 60

EDIT_AUDIT_FIELDS = ["title", "description", "category", "priority", "expected_outcome", "assigned_to",
                     "department", "location", "asset", "project", "estimated_minutes", "follow_up_action"]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def can_view(user, task):
    return is_it_staff(user)


def can_modify(user, task):
    """Administrators may change any task; IT officers their own or ones they created."""
    if has_role(user, Role.ADMIN):
        return True
    return has_role(user, Role.IT_OFFICER) and user.pk in (task.assigned_to_id, task.created_by_id)


def require_modify(user, task):
    if not can_modify(user, task):
        audit.log(AuditLog.Action.ACCESS_DENIED, task, actor=user, message="Attempted to change a task without permission")
        raise PermissionDenied("You can only change tasks assigned to you or created by you.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _diary(task, user, event, title, *, description="", outcome="", time_entry=None, when=None):
    return ActivityLog.objects.create(
        user=user, source=ActivityLog.Source.AUTO, event=event,
        activity_type=task.category.activity_type,
        occurred_at=when or timezone.now(), title=title[:200], description=description, outcome=outcome,
        department=task.department, location=task.location, task=task, time_entry=time_entry, created_by=user,
        asset=task.asset, project=task.project,
    )


def _comment(task, user, kind, body, old_status="", new_status=""):
    return TaskComment.objects.create(task=task, author=user, kind=kind, body=body,
                                      old_status=old_status, new_status=new_status)


def _assign_number(task):
    year = timezone.localdate().year
    task.number = f"TSK-{year}-{Sequence.next('TSK', year):05d}"


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------
@transaction.atomic
def create_task(task, user, *, log=True):
    """Save a new (unsaved) Task instance with a number and history."""
    task.created_by = task.created_by if task.created_by_id else user
    task.original_planned_date = task.planned_date
    _assign_number(task)
    task.full_clean()
    task.save()
    if log:
        audit.log(AuditLog.Action.CREATE, task, actor=user,
                  message=f"Task planned for {task.planned_date:%d %b %Y}, assigned to {task.assigned_to.get_username()}")
    return task


@transaction.atomic
def update_task(task, before, user):
    require_modify(user, before)
    task.full_clean()
    task.save()
    changes = audit.diff_instances(before, task, EDIT_AUDIT_FIELDS)
    if changes:
        action = AuditLog.Action.ASSIGN if list(changes) == ["assigned_to"] else AuditLog.Action.UPDATE
        audit.log(action, task, actor=user, changes=changes, message="Task details edited")
        if "assigned_to" in changes:
            _comment(task, user, TaskComment.Kind.SYSTEM,
                     f"Reassigned from {changes['assigned_to'][0]} to {changes['assigned_to'][1]}.")
    return task


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------
@transaction.atomic
def change_status(task, new_status, user, *, reason="", completion_notes="", follow_up_action=None):
    require_modify(user, task)
    task = Task.objects.select_for_update(of=("self",)).get(pk=task.pk)
    old = task.status
    if new_status not in Task.Status.values:
        raise ValidationError("Unknown status.")
    if new_status == old:
        raise ValidationError("The task already has this status.")
    reason = (reason or "").strip()
    reopening = old in Task.CLOSED_STATUSES and new_status not in Task.CLOSED_STATUSES

    if new_status in Task.REASON_REQUIRED_STATUSES and not reason:
        raise ValidationError({"reason": f"Give a reason for marking the task “{Task.Status(new_status).label}”."})
    if reopening and not reason:
        raise ValidationError({"reason": "Give a reason for reopening the task."})
    if new_status == Task.Status.COMPLETED and not (completion_notes or "").strip():
        raise ValidationError({"completion_notes": "Describe what was done before completing the task."})

    now = timezone.now()
    if new_status in Task.CLOSED_STATUSES or new_status in Task.REASON_REQUIRED_STATUSES:
        _stop_running_timer_for_task(task, user)

    task.status = new_status
    if new_status == Task.Status.IN_PROGRESS and not task.actual_start:
        task.actual_start = now
    if new_status == Task.Status.COMPLETED:
        task.completed_at = now
        task.completion_notes = completion_notes.strip()
        if not task.actual_start:
            task.actual_start = now
    elif reopening:
        task.completed_at = None
    if reason and new_status != Task.Status.COMPLETED:
        task.delay_reason = reason
    if follow_up_action is not None:
        task.follow_up_action = follow_up_action.strip()
    task.save()

    label_old, label_new = Task.Status(old).label, Task.Status(new_status).label
    body = f"{label_old} → {label_new}."
    if reason:
        body += f" Reason: {reason}"
    if new_status == Task.Status.COMPLETED:
        body += f" Completion notes: {task.completion_notes}"
    _comment(task, user, TaskComment.Kind.STATUS, body, old, new_status)

    if new_status == Task.Status.COMPLETED:
        event, title = ActivityLog.Event.TASK_COMPLETED, f"Completed {task.number}: {task.title}"
    elif reopening:
        event, title = ActivityLog.Event.TASK_REOPENED, f"Reopened {task.number}: {task.title}"
    elif new_status == Task.Status.IN_PROGRESS:
        event, title = ActivityLog.Event.TASK_STARTED, f"Started {task.number}: {task.title}"
    else:
        event, title = ActivityLog.Event.TASK_STATUS, f"{task.number} marked {label_new}: {task.title}"
    _diary(task, user, event, title, description=reason, outcome=task.completion_notes if new_status == Task.Status.COMPLETED else "")
    audit.log(AuditLog.Action.STATUS_CHANGE, task, actor=user, changes={"status": [old, new_status]},
              message=reason[:300])
    return task


@transaction.atomic
def reschedule(task, user, *, new_date, reason, new_start=None, new_end=None):
    require_modify(user, task)
    task = Task.objects.select_for_update(of=("self",)).get(pk=task.pk)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for rescheduling."})
    if task.is_closed:
        raise ValidationError("Completed or cancelled tasks cannot be rescheduled. Reopen it first.")
    if new_date == task.planned_date and new_start == task.scheduled_start and new_end == task.scheduled_end:
        raise ValidationError({"new_date": "Choose a different date or time."})
    if new_start and new_end and new_end <= new_start:
        raise ValidationError({"new_end": "The end time must be after the start time."})

    TaskScheduleChange.objects.create(task=task, old_date=task.planned_date, new_date=new_date,
                                      old_start=task.scheduled_start, new_start=new_start, reason=reason,
                                      changed_by=user)
    old_date = task.planned_date
    task.planned_date, task.scheduled_start, task.scheduled_end = new_date, new_start, new_end
    task.delay_reason = reason
    task.save()
    body = f"Moved from {old_date:%d %b %Y} to {new_date:%d %b %Y}" + (f" {new_start:%H:%M}" if new_start else "")
    _comment(task, user, TaskComment.Kind.RESCHEDULE, f"{body}. Reason: {reason}")
    _diary(task, user, ActivityLog.Event.TASK_RESCHEDULED, f"Rescheduled {task.number}: {task.title}",
           description=f"{body}. Reason: {reason}")
    audit.log(AuditLog.Action.UPDATE, task, actor=user,
              changes={"planned_date": [str(old_date), str(new_date)]}, message=f"Rescheduled: {reason}"[:500])
    return task


def bulk_reschedule(tasks, user, *, new_date, reason):
    """Carry several unfinished tasks over to another day. Returns (moved, skipped)."""
    moved, skipped = [], []
    for task in tasks:
        try:
            reschedule(task, user, new_date=new_date, reason=reason,
                       new_start=task.scheduled_start, new_end=task.scheduled_end)
            moved.append(task)
        except (ValidationError, PermissionDenied):
            skipped.append(task)
    return moved, skipped


@transaction.atomic
def add_comment(task, user, body):
    if not can_view(user, task):
        raise PermissionDenied
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Write a comment."})
    return _comment(task, user, TaskComment.Kind.COMMENT, body)


# ---------------------------------------------------------------------------
# Time tracking (storage rules live in apps.diary.timekeeping)
# ---------------------------------------------------------------------------
def running_timer(user):
    return timekeeping.running_entry(user)


def _stop_running_timer_for_task(task, user):
    from apps.helpdesk.models import TicketTimeEntry

    for qs in (TaskTimeEntry.objects.filter(task=task), TicketTimeEntry.objects.filter(via_task=task)):
        entry = qs.select_for_update(of=("self",)).filter(ended_at__isnull=True, is_voided=False).first()
        if entry:
            timekeeping.close_entry(entry, entry.user)


@transaction.atomic
def start_timer(task, user):
    """Start timing work on ``task``. Any other running timer of this user is stopped first."""
    require_modify(user, task)
    task = Task.objects.select_for_update(of=("self",)).select_related("related_ticket", "category").get(pk=task.pk)
    if task.is_closed:
        raise ValidationError("Reopen the task before recording more work on it.")
    entry, stopped = timekeeping.start(user, task=task)
    if task.status != Task.Status.IN_PROGRESS:
        old = task.status
        task.status = Task.Status.IN_PROGRESS
        task.actual_start = task.actual_start or entry.started_at
        task.save(update_fields=["status", "actual_start", "updated_at"])
        _comment(task, user, TaskComment.Kind.STATUS, f"{Task.Status(old).label} → In progress (timer started).",
                 old, Task.Status.IN_PROGRESS)
        _diary(task, user, ActivityLog.Event.TASK_STARTED, f"Started {task.number}: {task.title}")
        audit.log(AuditLog.Action.STATUS_CHANGE, task, actor=user, changes={"status": [old, Task.Status.IN_PROGRESS]})
    return entry, stopped


@transaction.atomic
def stop_timer(user, note=""):
    entry = timekeeping.stop_running(user, note)
    if entry is None:
        raise ValidationError("No timer is running.")
    return entry


@transaction.atomic
def log_time(task, user, *, started_at, minutes, note=""):
    """Record time worked after the fact (stored on the ticket if the task is linked to one)."""
    require_modify(user, task)
    return timekeeping.record(user, started_at=started_at, minutes=minutes, note=note, task=task)


@transaction.atomic
def void_time_entry(entry, user, reason):
    task = entry.task if hasattr(entry, "task") else entry.via_task
    require_modify(user, task)
    if entry.user_id != user.pk and not has_role(user, Role.ADMIN):
        raise PermissionDenied("Only the person who recorded the time, or an administrator, can void it.")
    timekeeping.void(entry, user, reason)
    _comment(task, user, TaskComment.Kind.SYSTEM, f"Time entry of {entry.minutes} min voided. Reason: {entry.void_reason}")
    return entry


@transaction.atomic
def add_attachments(task, uploads, user, description=""):
    from apps.evidence.models import EvidenceFile

    from .models import TaskAttachment

    require_modify(user, task)
    saved = []
    for f, _ext, kind, content_type in uploads:
        att = TaskAttachment(task=task, original_name=f.name[:255], content_type=content_type, kind=kind, size=f.size,
                             sha256=EvidenceFile.fingerprint(f), description=description[:255], uploaded_by=user)
        att.file.save(f.name, f, save=False)
        att.save()
        audit.log(AuditLog.Action.UPLOAD, task, actor=user, message=f"Attachment uploaded: {att.original_name}")
        saved.append(att)
    _comment(task, user, TaskComment.Kind.SYSTEM, f"{len(saved)} file(s) attached"
             + (f": {description}" if description else "."))
    return saved


@transaction.atomic
def hide_attachment(att, user, reason):
    require_modify(user, att.task)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for removing this file."})
    att.is_deleted, att.deleted_by, att.deleted_at, att.delete_reason = True, user, timezone.now(), reason[:300]
    att.save()
    audit.log(AuditLog.Action.DELETE, att.task, actor=user,
              message=f"Attachment removed from view: {att.original_name}. Reason: {reason}"[:500])


@transaction.atomic
def link_to_ticket(task, ticket, user):
    """
    Link a task to a ticket. From now on the task's time is recorded on the
    ticket; time already recorded on the task is moved there (the original
    task entries are kept, voided, with a pointer to the ticket).
    """
    from apps.helpdesk.models import TicketTimeEntry

    require_modify(user, task)
    task = Task.objects.select_for_update(of=("self",)).get(pk=task.pk)
    if task.related_ticket_id:
        raise ValidationError("This task is already linked to a ticket.")
    _stop_running_timer_for_task(task, user)
    moved = 0
    for e in TaskTimeEntry.objects.select_for_update(of=("self",)).filter(task=task, is_voided=False, ended_at__isnull=False):
        TicketTimeEntry.objects.create(ticket=ticket, via_task=task, user=e.user, started_at=e.started_at,
                                       ended_at=e.ended_at, minutes=e.minutes, note=e.note, is_manual=e.is_manual)
        e.is_voided, e.void_reason, e.voided_by, e.voided_at = (
            True, f"Moved to ticket {ticket.reference}", user, timezone.now())
        e.save()
        moved += e.minutes
    task.related_ticket = ticket
    task.save(update_fields=["related_ticket", "updated_at"])
    note = f"Linked to ticket {ticket.reference}." + (f" {format_minutes(moved)} of recorded time moved to the ticket." if moved else "")
    _comment(task, user, TaskComment.Kind.SYSTEM, note)
    audit.log(AuditLog.Action.UPDATE, task, actor=user, changes={"related_ticket": ["", ticket.reference]}, message=note)
    return task


# ---------------------------------------------------------------------------
# Unplanned work / interruptions
# ---------------------------------------------------------------------------
@transaction.atomic
def record_unplanned(task, user, *, interrupted_task=None, start_timer_now=False, worked_from=None,
                     worked_minutes=None, completion_notes=""):
    """
    Record unplanned or emergency work as its own task. The interrupted task
    is not modified, apart from a note on its timeline.
    """
    task.is_unplanned = True
    if not task.planned_date:
        task.planned_date = timezone.localdate(worked_from) if worked_from else timezone.localdate()
    task.interrupted_task = interrupted_task
    task.status = Task.Status.NOT_STARTED
    create_task(task, user, log=False)
    audit.log(AuditLog.Action.CREATE, task, actor=user, message="Unplanned work recorded")
    detail = f" (interrupted {interrupted_task.number})" if interrupted_task else ""
    _diary(task, user, ActivityLog.Event.UNPLANNED, f"Unplanned work {task.number}: {task.title}{detail}",
           description=task.description)
    if interrupted_task:
        _comment(interrupted_task, user, TaskComment.Kind.INTERRUPTION,
                 f"Interrupted by unplanned work {task.number}: {task.title}")
    if worked_from and worked_minutes:
        log_time(task, user, started_at=worked_from, minutes=worked_minutes)
    if completion_notes:
        change_status(task, Task.Status.COMPLETED, user, completion_notes=completion_notes)
        task.refresh_from_db()
    elif start_timer_now:
        start_timer(task, user)
        task.refresh_from_db()
    return task


def timer_warning(entry):
    if entry.minutes >= LONG_TIMER_WARNING_MINUTES:
        return (f"The timer ran for {format_minutes(entry.minutes)}. If it was left running by mistake, "
                "void the entry and record the real time.")
    return None
