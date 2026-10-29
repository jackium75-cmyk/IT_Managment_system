"""
THE time-counting rule. Every report, dashboard and diary total must use
this module, so time is calculated the same way everywhere.

Counted time comes from exactly these sources, each minute counted once:

1. ``TaskTimeEntry`` rows that are finished and not voided. Time on a task
   that is linked to a ticket is never stored here: it is stored as a
   ``TicketTimeEntry`` with ``via_task`` set (see timekeeping.py).
2. ``TicketTimeEntry`` rows that are finished and not voided. They count as
   "support" work (or the linked task's category), and as unplanned work
   unless they were done through a planned task.
3. Manual ``ActivityLog`` entries (not voided). Their ``duration_minutes`` is
   their own time; they are never linked to a task's time entries.

Automatic diary entries never carry time. They only point at the
``TaskTimeEntry`` they describe.

An entry belongs to the day (hotel timezone) on which it *started*.
Overlapping entries for the same person are rejected when they are recorded
(see ``find_overlap``), so totals cannot exceed real elapsed time.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .choices import ActivityType


@dataclass
class TimeRecord:
    source: str  # "task" | "ticket" | "manual"
    user_id: int
    started_at: datetime
    minutes: int
    activity_type: str
    title: str
    url: str
    is_unplanned: bool = False
    task_id: int | None = None
    ticket_id: int | None = None
    project_id: int | None = None
    asset_id: int | None = None
    department_id: int | None = None

    @property
    def day(self) -> date:
        return timezone.localdate(self.started_at)


def time_records(start_dt, end_dt, *, user=None, department_id=None, category_id=None, activity_type=None):
    """All counted time records that started within [start_dt, end_dt]."""
    from apps.tasks.models import TaskTimeEntry

    from .models import ActivityLog

    records = []

    task_qs = TaskTimeEntry.objects.filter(
        is_voided=False, ended_at__isnull=False, started_at__range=(start_dt, end_dt)
    ).select_related("task", "task__category")
    if user is not None:
        task_qs = task_qs.filter(user=user)
    if department_id:
        task_qs = task_qs.filter(task__department_id=department_id)
    if category_id:
        task_qs = task_qs.filter(task__category_id=category_id)
    if activity_type:
        task_qs = task_qs.filter(task__category__activity_type=activity_type)
    for e in task_qs:
        records.append(TimeRecord(
            source="task", user_id=e.user_id, started_at=e.started_at, minutes=e.minutes,
            activity_type=e.task.category.activity_type, title=f"{e.task.number} {e.task.title}",
            url=reverse("tasks:detail", args=[e.task_id]), is_unplanned=e.task.is_unplanned,
            task_id=e.task_id, project_id=e.task.project_id, asset_id=e.task.asset_id,
            department_id=e.task.department_id,
        ))

    from apps.helpdesk.models import TicketTimeEntry

    ticket_qs = TicketTimeEntry.objects.filter(
        is_voided=False, ended_at__isnull=False, started_at__range=(start_dt, end_dt)
    ).select_related("ticket", "via_task", "via_task__category")
    if user is not None:
        ticket_qs = ticket_qs.filter(user=user)
    if department_id:
        ticket_qs = ticket_qs.filter(ticket__department_id=department_id)
    if category_id:
        ticket_qs = ticket_qs.filter(via_task__category_id=category_id)
    for e in ticket_qs:
        task = e.via_task
        kind = task.category.activity_type if task else ActivityType.SUPPORT
        if activity_type and kind != activity_type:
            continue
        records.append(TimeRecord(
            source="ticket", user_id=e.user_id, started_at=e.started_at, minutes=e.minutes, activity_type=kind,
            title=f"{e.ticket.reference} {e.ticket.subject}", url=e.ticket.get_absolute_url(),
            is_unplanned=task is None or task.is_unplanned,
            task_id=e.via_task_id, ticket_id=e.ticket_id,
            project_id=e.ticket.project_id or (task.project_id if task else None),
            asset_id=e.ticket.asset_id or (task.asset_id if task else None), department_id=e.ticket.department_id,
        ))

    if not category_id:  # manual entries have no task category
        manual_qs = ActivityLog.objects.current().filter(
            source=ActivityLog.Source.MANUAL, occurred_at__range=(start_dt, end_dt)
        )
        if user is not None:
            manual_qs = manual_qs.filter(user=user)
        if department_id:
            manual_qs = manual_qs.filter(department_id=department_id)
        if activity_type:
            manual_qs = manual_qs.filter(activity_type=activity_type)
        for a in manual_qs:
            records.append(TimeRecord(
                source="manual", user_id=a.user_id, started_at=a.occurred_at, minutes=a.duration_minutes,
                activity_type=a.activity_type, title=a.title, url=reverse("diary:detail", args=[a.pk]),
                is_unplanned=a.activity_type == ActivityType.EMERGENCY,
                project_id=a.project_id, asset_id=a.asset_id, ticket_id=a.ticket_id, department_id=a.department_id,
            ))
    return sorted(records, key=lambda r: r.started_at)


def summarize(records):
    by_activity = defaultdict(int)
    by_day = defaultdict(int)
    planned = unplanned = 0
    for r in records:
        by_activity[r.activity_type] += r.minutes
        by_day[r.day] += r.minutes
        if r.is_unplanned:
            unplanned += r.minutes
        else:
            planned += r.minutes
    labels = dict(ActivityType.choices)
    return {
        "total_minutes": planned + unplanned,
        "planned_minutes": planned,
        "unplanned_minutes": unplanned,
        "by_activity": sorted(((labels.get(k, k), k, v) for k, v in by_activity.items()), key=lambda x: -x[2]),
        "by_day": dict(by_day),
        "count": len(records),
    }


def find_overlap(user, start, end, *, exclude_time_entry=None, exclude_activity=None):
    """
    Return a description of an existing counted record for ``user`` that overlaps
    [start, end), or None. Running timers are treated as running until now.
    """
    from apps.helpdesk.models import TicketTimeEntry
    from apps.tasks.models import TaskTimeEntry

    from .models import ActivityLog

    now = timezone.now()
    for model, label in ((TaskTimeEntry, lambda e: e.task.number), (TicketTimeEntry, lambda e: e.ticket.reference)):
        entries = model.objects.filter(user=user, is_voided=False, started_at__lt=end).filter(
            Q(ended_at__gt=start) | Q(ended_at__isnull=True))
        if exclude_time_entry is not None and isinstance(exclude_time_entry, model):
            entries = entries.exclude(pk=exclude_time_entry.pk)
        for e in entries:
            if e.ended_at is None and now <= start:
                continue
            local = timezone.localtime(e.started_at)
            return f"time already recorded on {label(e)} from {local:%d %b %H:%M}"

    manual = ActivityLog.objects.current().filter(
        user=user, source=ActivityLog.Source.MANUAL, occurred_at__lt=end,
        occurred_at__gt=start - timedelta(minutes=24 * 60),  # longest allowed entry
    )
    if exclude_activity is not None:
        manual = manual.exclude(pk=exclude_activity.pk)
    for a in manual:
        if a.occurred_at + timedelta(minutes=a.duration_minutes) > start:
            local = timezone.localtime(a.occurred_at)
            return f"diary entry “{a.title}” from {local:%d %b %H:%M}"
    return None


def format_minutes(minutes):
    minutes = int(minutes or 0)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"
