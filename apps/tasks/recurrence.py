"""
Generation of tasks from recurring schedules.

Tasks are generated ``HORIZON_DAYS`` ahead so they appear in the planner.
Generation is idempotent (unique recurrence + date) and runs:
* automatically when the planner, task list or dashboard is opened, and
* from ``python manage.py generate_recurring_tasks`` (e.g. Windows Task Scheduler).

Future occurrences nobody has touched yet are "projections": when the schedule
is edited or deactivated they are removed and regenerated. Anything that has
been worked on, commented on or rescheduled is never removed.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.core import audit
from apps.core.models import AuditLog

from .models import RecurringTask, Task, TaskComment, TaskScheduleChange, TaskTimeEntry

HORIZON_DAYS = 14
_CACHE_KEY = "tasks:recurrence-generated:{}"


def generate_for(template, until=None):
    """Create missing occurrences of ``template`` up to ``until``. Returns the number created."""
    from .services import create_task

    today = timezone.localdate()
    until = until or today + timedelta(days=HORIZON_DAYS)
    if template.end_date:
        until = min(until, template.end_date)
    first = template.start_date
    if template.generated_until:
        first = max(first, template.generated_until + timedelta(days=1))
    # Never back-fill days before the schedule was created.
    first = max(first, timezone.localdate(template.created_at))

    created = 0
    day = first
    while day <= until:
        if template.occurs_on(day) and not Task.objects.filter(recurrence=template, occurrence_date=day).exists():
            task = Task(
                title=template.title, description=template.description, expected_outcome=template.expected_outcome,
                category=template.category, priority=template.priority, assigned_to=template.assigned_to,
                department=template.department, location=template.location, planned_date=day,
                scheduled_start=template.scheduled_start, scheduled_end=template.scheduled_end,
                estimated_minutes=template.estimated_minutes, recurrence=template, occurrence_date=day,
                created_by=template.created_by, status=Task.Status.PLANNED,
            )
            try:
                with transaction.atomic():
                    create_task(task, template.created_by, log=False)
                created += 1
            except IntegrityError:
                pass  # generated concurrently by another request
        day += timedelta(days=1)
    if until >= first:
        RecurringTask.objects.filter(pk=template.pk).update(generated_until=until)
        template.generated_until = until
    return created


def generate_all(until=None):
    total = 0
    for template in RecurringTask.objects.filter(is_active=True).select_related("category", "assigned_to"):
        total += generate_for(template, until)
    return total


def ensure_generated():
    """Cheap call for views: generate at most once per day per process cache."""
    key = _CACHE_KEY.format(timezone.localdate().isoformat())
    if cache.get(key):
        return 0
    created = generate_all()
    cache.set(key, True, 60 * 60)
    return created


def untouched_future_occurrences(template):
    today = timezone.localdate()
    return Task.objects.filter(recurrence=template, planned_date__gt=today, status=Task.Status.PLANNED).exclude(
        Exists(TaskTimeEntry.objects.filter(task=OuterRef("pk")))
    ).exclude(Exists(TaskComment.objects.filter(task=OuterRef("pk")))).exclude(
        Exists(TaskScheduleChange.objects.filter(task=OuterRef("pk")))
    ).exclude(Exists(Task.objects.filter(interrupted_task=OuterRef("pk"))))


@transaction.atomic
def refresh_template(template, user):
    """After an edit or deactivation: drop untouched future projections and regenerate."""
    removed = 0
    for task in untouched_future_occurrences(template):
        if task.activities.exists():
            continue
        task.delete()
        removed += 1
    # Reconsider today onwards; existing occurrences are never duplicated.
    restart = timezone.localdate() - timedelta(days=1)
    RecurringTask.objects.filter(pk=template.pk).update(generated_until=restart)
    template.generated_until = restart
    created = generate_for(template) if template.is_active else 0
    if removed or created:
        audit.log(AuditLog.Action.UPDATE, template, actor=user,
                  message=f"Schedule refreshed: {removed} future task(s) removed, {created} generated")
    cache.delete(_CACHE_KEY.format(timezone.localdate().isoformat()))
    return removed, created
