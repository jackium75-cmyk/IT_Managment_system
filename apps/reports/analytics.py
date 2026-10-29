"""
Workload and performance analytics.

Fairness rules built in:
* resolution times exclude time waiting on requesters, vendors, parts and approvals;
* waiting and blocked work is shown separately as "outside IT's control";
* figures are broken down by priority, because a critical outage is not
  comparable with a password reset;
* the per-person view lists several measures side by side, alphabetically —
  nobody is ranked by the number of tickets closed.
"""
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median

from django.utils import timezone

from apps.diary.timecalc import summarize

from . import sections as S
from .context import ReportContext

AGE_BUCKETS = [("Under 1 day", 0, 1), ("1–3 days", 1, 3), ("3–7 days", 3, 7), ("1–4 weeks", 7, 28),
               ("Over 4 weeks", 28, 10 ** 6)]


def _pct(n, d):
    return round(100 * n / d) if d else None


def _sub(ctx, start, end):
    return ReportContext(kind="analytics", start=start, end=end, user=ctx.user, department_id=ctx.department_id,
                         technician_id=ctx.technician_id, priority=ctx.priority,
                         ticket_category_id=ctx.ticket_category_id, task_category_id=ctx.task_category_id)


def recurring_incident_rate(ctx):
    """Share of incidents whose asset (or, without an asset, requester + category) had an incident in the prior 30 days."""
    from apps.helpdesk.models import Ticket

    incidents = list(S.tickets_received(ctx).filter(ticket_type="incident").order_by("created_at"))
    if not incidents:
        return None, 0, 0
    repeats = 0
    for t in incidents:
        earlier = Ticket.objects.filter(ticket_type="incident", created_at__lt=t.created_at,
                                        created_at__gte=t.created_at - timedelta(days=30)).exclude(pk=t.pk)
        earlier = earlier.filter(asset_id=t.asset_id) if t.asset_id else earlier.filter(
            requester_id=t.requester_id, category_id=t.category_id)
        if earlier.exists():
            repeats += 1
    return _pct(repeats, len(incidents)), repeats, len(incidents)


def backlog(ctx, at=None):
    from apps.tasks.models import Task

    at = at or timezone.now()
    tickets = list(ctx.tickets().open().filter(created_at__lte=at))
    buckets = Counter()
    for t in tickets:
        age = (at - t.created_at).total_seconds() / 86400
        for label, lo, hi in AGE_BUCKETS:
            if lo <= age < hi:
                buckets[label] += 1
    waiting = sum(1 for t in tickets if t.status == "waiting")
    overdue_tasks = ctx.tasks().overdue(timezone.localdate(at))
    task_waiting = overdue_tasks.filter(status__in=Task.EXTERNAL_WAIT_STATUSES + (Task.Status.BLOCKED,)).count()
    return {"tickets": len(tickets), "buckets": [(label, buckets.get(label, 0)) for label, _l, _h in AGE_BUCKETS],
            "waiting": waiting, "overdue_tasks": overdue_tasks.count(), "tasks_waiting": task_waiting,
            "oldest_days": round(max(((at - t.created_at).total_seconds() / 86400 for t in tickets), default=0), 1)}


def weekly_trend(ctx, weeks=12):
    from apps.helpdesk.models import Ticket

    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    rows = []
    for i in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        end = start + timedelta(days=6)
        sub = _sub(ctx, start, min(end, today))
        received = S.tickets_received(sub).count()
        resolved = S.tickets_resolved(sub).count()
        open_at_end = sub.tickets().filter(created_at__lte=sub.end_dt).exclude(
            resolved_at__lte=sub.end_dt).exclude(status=Ticket.Status.CLOSED, closed_at__lte=sub.end_dt).count()
        s = summarize(sub.time_records())
        tf = S.task_figures(sub, sub.start, sub.end)
        rows.append({"label": start.strftime("%d %b"), "received": received, "resolved": resolved,
                     "backlog": open_at_end, "hours": S.hours(s["total_minutes"]),
                     "unplanned_hours": S.hours(s["unplanned_minutes"]), "planned": tf["planned"],
                     "planned_done": tf["planned_done"], "interruptions": tf["unplanned"]})
    return rows


def insights(trend, ctx):
    """Plain-language statements about where workload is changing (last 4 weeks vs the 4 before)."""
    out = []
    if len(trend) < 8:
        return out
    recent, before = trend[-4:], trend[-8:-4]

    def change(key, label, unit=""):
        a, b = sum(r[key] for r in before), sum(r[key] for r in recent)
        if a >= 5 and abs(b - a) / a >= 0.2:
            direction = "rose" if b > a else "fell"
            out.append(f"{label} {direction} by {round(abs(b - a) / a * 100)}% ({a:g}{unit} → {b:g}{unit}) in the last "
                       f"4 weeks compared with the 4 weeks before.")

    change("received", "Requests received")
    change("interruptions", "Unplanned work items")
    change("unplanned_hours", "Hours of unplanned work", " h")
    if recent[-1]["backlog"] > before[-1]["backlog"] + 3:
        out.append(f"The open-ticket backlog grew from {before[-1]['backlog']} to {recent[-1]['backlog']}: "
                   "more requests are arriving than are being resolved.")
    rec_planned = sum(r["planned"] for r in recent)
    rec_done = sum(r["planned_done"] for r in recent)
    if rec_planned >= 5 and rec_done / rec_planned < 0.7:
        out.append(f"Only {round(100 * rec_done / rec_planned)}% of planned tasks were completed in the last 4 weeks; "
                   "check whether unplanned work is displacing the plan.")
    sub = _sub(ctx, timezone.localdate() - timedelta(days=27), timezone.localdate())
    top = Counter(S.tickets_received(sub).filter(ticket_type="incident").values_list("category__name", flat=True)).most_common(2)
    for name, n in top:
        if n >= 4:
            out.append(f"“{name}” produced {n} incidents in the last 4 weeks — a candidate for preventive work, "
                       "training or replacement.")
    return out


def officer_table(ctx):
    """Per-person workload with context; alphabetical, never ranked by tickets closed."""
    from apps.tasks.forms import it_staff_queryset

    records = ctx.time_records()
    by_user = defaultdict(list)
    for r in records:
        by_user[r.user_id].append(r)
    resolved = list(S.tickets_resolved(ctx).select_related("assigned_to"))
    rows = []
    for u in sorted(it_staff_queryset(), key=lambda x: (x.get_full_name() or x.username).lower()):
        if ctx.technician_id and u.pk != ctx.technician_id:
            continue
        recs = by_user.get(u.pk, [])
        s = summarize(recs)
        mine = [t for t in resolved if t.assigned_to_id == u.pk]
        mins = S.resolution_minutes(mine)
        paused = sum(t.paused_minutes for t in mine)
        elapsed = sum(max(1, int((t.resolved_at - t.created_at).total_seconds() // 60)) for t in mine)
        critical = sum(1 for t in mine if t.priority in ("critical", "high"))
        person = ReportContext(kind="analytics", start=ctx.start, end=ctx.end, user=ctx.user, technician_id=u.pk,
                               department_id=ctx.department_id)
        tf = S.task_figures(person, ctx.start, ctx.end)
        rows.append([u.get_full_name() or u.username, S.hours(s["total_minutes"]),
                     f"{_pct(s['unplanned_minutes'], s['total_minutes'])}%" if s["total_minutes"] else "—",
                     len(mine), f"{critical}" if mine else "—",
                     S.fmt_duration(median(mins) if mins else None),
                     f"{_pct(paused, elapsed)}%" if elapsed else "—",
                     f"{tf['planned_done']}/{tf['planned']}" if tf["planned"] else "—"])
    return rows


def build(ctx):
    from apps.maintenance.services import compliance

    tf = S.task_figures(ctx, ctx.start, ctx.end)
    records = ctx.time_records()
    s = summarize(records)
    resolved = list(S.tickets_resolved(ctx))
    mins = S.resolution_minutes(resolved)
    rate, repeats, incidents = recurring_incident_rate(ctx)
    comp = compliance(ctx.start, ctx.end, qs=ctx.maintenance())
    by_priority = []
    for p in ("critical", "high", "medium", "low"):
        m = S.resolution_minutes([t for t in resolved if t.priority == p])
        by_priority.append((p.capitalize(), len(m), S.fmt_duration(median(m) if m else None)))
    paused = sum(t.paused_minutes for t in resolved)
    elapsed = sum(max(1, int((t.resolved_at - t.created_at).total_seconds() // 60)) for t in resolved)
    groups = defaultdict(int)
    for label, key, minutes in s["by_activity"]:
        group = {"support": "Support", "maintenance": "Maintenance", "system_check": "Maintenance",
                 "security": "Maintenance", "project": "Projects", "administration": "Administration",
                 "meeting": "Administration", "documentation": "Administration", "training": "Administration",
                 "emergency": "Support"}.get(key, "Other")
        groups[group] += minutes
    trend = weekly_trend(ctx)
    return {
        "tf": tf, "time": s, "groups": sorted(groups.items(), key=lambda kv: -kv[1]),
        "planned_rate": _pct(tf["planned_done"], tf["planned"]), "late_rate": _pct(tf["late"], tf["planned_done"]),
        "handled": S.tickets_handled(ctx).count(), "resolved": len(resolved),
        "avg": S.fmt_duration(sum(mins) / len(mins) if mins else None), "median": S.fmt_duration(median(mins) if mins else None),
        "by_priority": by_priority, "waiting_share": _pct(paused, elapsed),
        "recurring": (rate, repeats, incidents), "backlog": backlog(ctx), "compliance": comp,
        "improvements": ctx.projects().filter(status="completed", completed_on__range=(ctx.start, ctx.end)).count(),
        "trend": trend, "insights": insights(trend, ctx),
        "officers": officer_table(ctx) if ctx.is_it else [],
    }
