"""
Timers and time entries for both tasks and tickets.

A person has at most one running timer across tasks AND tickets; starting a
new one stops the old one. Where time is stored follows the counting rule:

* task not linked to a ticket  -> TaskTimeEntry
* task linked to a ticket      -> TicketTimeEntry(via_task=task)
* ticket                        -> TicketTimeEntry

Every finished entry gets an automatic diary line (which carries no time
itself) and an audit record.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core import audit
from apps.core.models import AuditLog

from .choices import ActivityType
from .models import ActivityLog
from .timecalc import find_overlap, format_minutes

MAX_ENTRY_MINUTES = 24 * 60


def _models():
    from apps.helpdesk.models import TicketTimeEntry
    from apps.tasks.models import TaskTimeEntry

    return TaskTimeEntry, TicketTimeEntry


def is_ticket_entry(entry):
    return hasattr(entry, "ticket_id")


def subject_label(entry):
    if is_ticket_entry(entry):
        return f"{entry.ticket.reference}: {entry.ticket.subject}"
    return f"{entry.task.number}: {entry.task.title}"


def subject_url(entry):
    return entry.ticket.get_absolute_url() if is_ticket_entry(entry) else entry.task.get_absolute_url()


def running_entry(user, lock=False):
    for model in _models():
        qs = model.objects.filter(user=user, ended_at__isnull=True, is_voided=False)
        if lock:
            qs = qs.select_for_update(of=("self",))
        related = ("ticket", "via_task") if model.__name__ == "TicketTimeEntry" else ("task", "task__category")
        entry = qs.select_related(*related).first()
        if entry:
            return entry
    return None


def _diary_for(entry, user, when):
    if is_ticket_entry(entry):
        ticket = entry.ticket
        ActivityLog.objects.create(
            user=user, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.TIME_LOGGED,
            activity_type=entry.via_task.category.activity_type if entry.via_task_id else ActivityType.SUPPORT,
            occurred_at=when, title=f"Worked {format_minutes(entry.minutes)} on {subject_label(entry)}"[:200],
            description=entry.note, department=ticket.department, location=ticket.location,
            task=entry.via_task, ticket=ticket, ticket_time_entry=entry, created_by=user,
            asset=ticket.asset or (entry.via_task.asset if entry.via_task_id else None),
            project=ticket.project or (entry.via_task.project if entry.via_task_id else None),
        )
    else:
        task = entry.task
        ActivityLog.objects.create(
            user=user, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.TIME_LOGGED,
            activity_type=task.category.activity_type, occurred_at=when,
            title=f"Worked {format_minutes(entry.minutes)} on {subject_label(entry)}"[:200], description=entry.note,
            department=task.department, location=task.location, task=task, time_entry=entry, created_by=user,
            asset=task.asset, project=task.project,
        )


def _audit_target(entry):
    return entry.ticket if is_ticket_entry(entry) else entry.task


def close_entry(entry, user, end=None):
    end = end or timezone.now()
    if end <= entry.started_at:
        end = entry.started_at + timedelta(minutes=1)
    entry.ended_at = end
    entry.minutes = max(1, round((end - entry.started_at).total_seconds() / 60))  # nearest minute
    entry.save(update_fields=["ended_at", "minutes"])
    _diary_for(entry, user, entry.started_at)
    audit.log(AuditLog.Action.UPDATE, _audit_target(entry), actor=user, message=f"Timer stopped: {entry.minutes} min")
    return entry


def stop_running(user, note=""):
    """Stop the user's running timer (task or ticket). Returns the closed entry or None."""
    entry = running_entry(user, lock=True)
    if entry is None:
        return None
    if note:
        entry.note = note.strip()[:300]
        entry.save(update_fields=["note"])
    return close_entry(entry, user)


def start(user, *, task=None, ticket=None):
    """
    Start a timer on a task or ticket, stopping any other running timer first.
    Returns (new_entry, stopped_entry_or_None). Caller handles permissions and
    status changes and must run inside a transaction.
    """
    TaskTimeEntry, TicketTimeEntry = _models()
    current = running_entry(user, lock=True)
    if current is not None:
        same = (task is not None and getattr(current, "task_id", None) == task.pk) or \
               (task is not None and getattr(current, "via_task_id", None) == task.pk) or \
               (ticket is not None and getattr(current, "ticket_id", None) == ticket.pk and not current.via_task_id)
        if same:
            raise ValidationError("A timer is already running on this item.")
        current = close_entry(current, user)
    now = timezone.now()
    if task is not None and task.related_ticket_id:
        entry = TicketTimeEntry.objects.create(ticket=task.related_ticket, via_task=task, user=user, started_at=now)
    elif task is not None:
        entry = TaskTimeEntry.objects.create(task=task, user=user, started_at=now)
    else:
        entry = TicketTimeEntry.objects.create(ticket=ticket, user=user, started_at=now)
    return entry, current


def record(user, *, started_at, minutes, note="", task=None, ticket=None):
    """Record time after the fact (validated: duration, not in the future, no overlap)."""
    TaskTimeEntry, TicketTimeEntry = _models()
    if not 1 <= minutes <= MAX_ENTRY_MINUTES:
        raise ValidationError({"minutes": "Enter between 1 minute and 24 hours."})
    ended_at = started_at + timedelta(minutes=minutes)
    if ended_at > timezone.now() + timedelta(minutes=1):
        raise ValidationError({"started_at": "Time cannot be recorded in the future."})
    clash = find_overlap(user, started_at, ended_at)
    if clash:
        raise ValidationError({"started_at": f"This overlaps {clash}. Time is never counted twice."})
    common = dict(user=user, started_at=started_at, ended_at=ended_at, minutes=minutes, note=note.strip()[:300],
                  is_manual=True)
    if task is not None and task.related_ticket_id:
        entry = TicketTimeEntry.objects.create(ticket=task.related_ticket, via_task=task, **common)
    elif task is not None:
        entry = TaskTimeEntry.objects.create(task=task, **common)
    else:
        entry = TicketTimeEntry.objects.create(ticket=ticket, **common)
    _diary_for(entry, user, started_at)
    audit.log(AuditLog.Action.CREATE, _audit_target(entry), actor=user, message=f"Time recorded: {minutes} min")
    return entry


def void(entry, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for voiding this time entry."})
    if entry.is_voided:
        raise ValidationError("This entry is already void.")
    entry.is_voided, entry.void_reason, entry.voided_by, entry.voided_at = True, reason[:300], user, timezone.now()
    if entry.ended_at is None:
        entry.ended_at = timezone.now()
    entry.save()
    audit.log(AuditLog.Action.DELETE, _audit_target(entry), actor=user, changes={"time_entry": [entry.minutes, "void"]},
              message=f"Time entry voided: {reason}"[:500])
    return entry
