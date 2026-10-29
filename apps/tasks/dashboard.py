"""Phase 2 dashboard metrics: daily work, task outcomes and recorded time."""
from datetime import datetime, time
from urllib.parse import urlencode

from django.db.models import Count, F
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import IT_STAFF_ROLES
from apps.dashboard.registry import card_provider, chart_provider
from apps.diary.timecalc import format_minutes, summarize, time_records

from .models import Task

IT = IT_STAFF_ROLES


def _task_filters(filters):
    """Apply the dashboard's department / category / priority / officer filters to tasks."""
    qs = Task.objects.all()
    x = filters.extra
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    if x.get("category"):
        qs = qs.filter(category_id=x["category"])
    if x.get("priority"):
        qs = qs.filter(priority=x["priority"])
    if x.get("officer"):
        qs = qs.filter(assigned_to_id=x["officer"])
    return qs


def _link(filters, **params):
    x = filters.extra
    base = {"view": "all", "department": filters.department_id, "category": x.get("category"),
            "priority": x.get("priority"), "assigned": x.get("officer")}
    base.update(params)
    return reverse("tasks:list") + "?" + urlencode({k: v for k, v in base.items() if v not in (None, "")})


def _officer(filters):
    from django.contrib.auth import get_user_model

    oid = filters.extra.get("officer")
    return get_user_model().objects.filter(pk=oid).first() if oid else None


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(day, time.min), tz),
            timezone.make_aware(datetime.combine(day, time.max), tz))


@card_provider("Today's work", order=1, roles=IT)
def today_cards(filters):
    from .recurrence import ensure_generated

    ensure_generated()
    today = timezone.localdate()
    qs = _task_filters(filters)
    t = today.isoformat()
    start, end = _day_bounds(today)
    records = time_records(start, end, user=_officer(filters), department_id=filters.department_id,
                           category_id=filters.extra.get("category"))
    minutes = summarize(records)["total_minutes"]
    return [
        {"label": "Scheduled for today", "value": qs.filter(planned_date=today, is_unplanned=False).count(),
         "icon": "calendar-day", "tone": "primary", "url": _link(filters, view="today", unplanned="")},
        {"label": "Completed today", "value": qs.filter(status=Task.Status.COMPLETED, completed_at__range=(start, end)).count(),
         "icon": "check-circle", "tone": "success", "url": _link(filters, done=t),
         "help": "Tasks completed today, whatever day they were planned for."},
        {"label": "In progress", "value": qs.filter(status=Task.Status.IN_PROGRESS).count(), "icon": "play-circle",
         "tone": "info", "url": _link(filters, status="in_progress")},
        {"label": "Overdue", "value": qs.overdue(today).count(), "icon": "exclamation-triangle", "tone": "danger",
         "url": _link(filters, view="overdue"), "help": "Open tasks whose planned date has passed."},
        {"label": "Unplanned work today", "value": qs.filter(is_unplanned=True, planned_date=today).count(),
         "icon": "lightning", "tone": "warning", "url": _link(filters, view="unplanned", start=t, end=t),
         "help": "Emergencies and interruptions recorded today."},
        {"label": "Time recorded today", "value": format_minutes(minutes), "icon": "stopwatch", "tone": "secondary",
         "url": reverse("diary:list") + f"?start={t}&end={t}"},
    ]


@card_provider("Tasks in selected period", order=2, roles=IT)
def period_cards(filters):
    qs = _task_filters(filters).filter(planned_date__range=(filters.start, filters.end))
    status = filters.extra.get("status")
    if status:
        qs = qs.filter(status=status)
    planned = qs.filter(is_unplanned=False)
    p = {"start": filters.start.isoformat(), "end": filters.end.isoformat(), "status": status}
    planned_n = planned.count()
    done_n = planned.filter(status=Task.Status.COMPLETED).count()
    late = planned.filter(status=Task.Status.COMPLETED).annotate(d=TruncDate("completed_at")).filter(
        d__gt=F("planned_date")).count()
    rescheduled = qs.exclude(planned_date=F("original_planned_date")).count()
    waiting = qs.filter(status__in=Task.EXTERNAL_WAIT_STATUSES).count()
    return [
        {"label": "Planned tasks", "value": planned_n, "icon": "list-task", "tone": "primary", "url": _link(filters, **p)},
        {"label": "Planned tasks completed", "value": done_n, "icon": "check2-all", "tone": "success",
         "url": _link(filters, **{**p, "status": "completed"})},
        {"label": "Completion rate", "value": f"{round(100 * done_n / planned_n)}%" if planned_n else "—",
         "icon": "percent", "tone": "success", "help": "Planned tasks completed ÷ planned tasks (unplanned work excluded)."},
        {"label": "Completed late", "value": late, "icon": "hourglass-bottom", "tone": "warning",
         "url": _link(filters, **{**p, "late": "1"}), "help": "Completed after their planned date."},
        {"label": "Rescheduled", "value": rescheduled, "icon": "arrow-repeat", "tone": "secondary",
         "help": "Tasks whose date differs from the date first planned."},
        {"label": "Waiting on others", "value": waiting, "icon": "pause-circle", "tone": "secondary",
         "help": "Waiting for information or parts — delays outside IT's control."},
    ]


@chart_provider(order=1, roles=IT)
def completed_chart(filters):
    qs = _task_filters(filters).filter(status=Task.Status.COMPLETED,
                                       completed_at__range=(filters.start_dt, filters.end_dt))
    rows = dict(qs.annotate(d=TruncDate("completed_at")).values("d").annotate(n=Count("id")).values_list("d", "n"))
    unplanned = dict(qs.filter(is_unplanned=True).annotate(d=TruncDate("completed_at")).values("d")
                     .annotate(n=Count("id")).values_list("d", "n"))
    days = filters.days()
    return [{
        "id": "chart-tasks-completed", "title": "Tasks completed per day", "type": "bar",
        "labels": [d.strftime("%d %b") for d in days],
        "datasets": [
            {"label": "Planned", "data": [rows.get(d, 0) - unplanned.get(d, 0) for d in days], "stack": "s"},
            {"label": "Unplanned", "data": [unplanned.get(d, 0) for d in days], "stack": "s"},
        ],
        "stacked": True,
    }]


@chart_provider(order=2, roles=IT)
def time_charts(filters):
    records = time_records(filters.start_dt, filters.end_dt, user=_officer(filters),
                           department_id=filters.department_id, category_id=filters.extra.get("category"))
    s = summarize(records)
    if not s["total_minutes"]:
        return [{"id": "chart-time-empty", "title": "Recorded time", "empty": "No time recorded in this period."}]
    return [
        {"id": "chart-planned-unplanned", "title": "Planned vs unplanned work (hours)", "type": "doughnut",
         "labels": ["Planned", "Unplanned"],
         "datasets": [{"label": "Hours", "data": [round(s["planned_minutes"] / 60, 1),
                                                   round(s["unplanned_minutes"] / 60, 1)]}],
         "help": f"Total {format_minutes(s['total_minutes'])} recorded. Each minute is counted once."},
        {"id": "chart-time-activity", "title": "Hours by activity type", "type": "bar", "horizontal": True,
         "labels": [label for label, _k, _m in s["by_activity"]],
         "datasets": [{"label": "Hours", "data": [round(m / 60, 1) for _l, _k, m in s["by_activity"]]}]},
    ]
