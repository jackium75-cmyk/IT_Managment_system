"""Daily, weekly, monthly, annual and custom IT reports, built from recorded data only."""
from datetime import date, timedelta
from statistics import median

from django.utils import timezone

from apps.core.models import SystemSetting
from apps.diary.timecalc import format_minutes

from . import sections as S
from .structure import Metric, Report, Section

DEFINITIONS = [
    ("Planned task", "A task scheduled in advance (not recorded as unplanned work)."),
    ("Completed late", "Completed after its planned date (the latest agreed date)."),
    ("Unplanned work", "Emergencies, walk-up requests and other work recorded as unplanned; ticket time counts as "
                       "unplanned unless it was done through a planned task."),
    ("Tickets handled", "Tickets on which IT changed the status, replied, added a note or recorded time in the period."),
    ("Resolution time", "From report to resolution, excluding time the ticket spent waiting on the requester, a vendor, "
                        "parts or an approval."),
    ("Recorded time", "Time logged by IT staff. Each minute is counted once across tasks, tickets and diary entries."),
    ("Verified", "Confirmed by the requester (ticket confirmed fixed) or by a manager (project benefits)."),
]


def _report(ctx, title, period_label):
    cfg = SystemSetting.get()
    return Report(kind=ctx.kind, title=title, period_label=period_label, filters=ctx.filter_description(),
                  generated_at=timezone.now(), generated_by=ctx.user.get_full_name() or ctx.user.username,
                  hotel_name=cfg.hotel_name, definitions=list(DEFINITIONS))


def _finish(report, ctx):
    if ctx.is_manager:
        report.sections = [s for s in report.sections if not s.it_only]
    return report


def _ticket_metrics(ctx):
    received = S.tickets_received(ctx)
    resolved = list(S.tickets_resolved(ctx))
    mins = S.resolution_minutes(resolved)
    r1, r2 = S.sla_rates(ctx)
    return received, resolved, [
        Metric("Requests received", received.count()),
        Metric("Requests resolved", len(resolved)),
        Metric("Resolved and confirmed by requester", sum(1 for t in resolved if t.confirmed_by_requester), "verified"),
        Metric("Received in period, still open", received.exclude(status__in=["resolved", "closed"]).count()),
        Metric("Median resolution time", S.fmt_duration(median(mins) if mins else None),
               help="Waiting time excluded."),
        Metric("First response on time", f"{r1}%" if r1 is not None else "—"),
        Metric("Resolved within target", f"{r2}%" if r2 is not None else "—"),
    ]


# ---------------------------------------------------------------------------
# Daily
# ---------------------------------------------------------------------------
def build_daily(ctx):
    from apps.tasks.models import Task

    day = ctx.start
    ctx.end = day
    r = _report(ctx, "Daily IT activity report", day.strftime("%A %d %B %Y"))
    tf = S.task_figures(ctx, day, day)
    handled = S.tickets_handled(ctx)
    resolved = S.tickets_resolved(ctx)
    records = ctx.time_records()
    summary, time_table, time_chart = S.time_sections(ctx, records)
    maint = ctx.maintenance().filter(status="completed", completed_on=day)
    r.add(Section("Summary", "metrics", metrics=[
        Metric("Planned tasks", tf["planned"]),
        Metric("Planned tasks completed", tf["planned_done"]),
        Metric("Unplanned work items", tf["unplanned"]),
        Metric("Tickets handled", handled.count()),
        Metric("Problems resolved", resolved.count()),
        Metric("Confirmed fixed by requester", resolved.filter(confirmed_by_requester=True).count(), "verified"),
        Metric("Maintenance visits done", maint.count()),
        Metric("Time recorded", format_minutes(summary["total_minutes"]), "reported"),
        Metric("Estimated workload of planned tasks", format_minutes(tf["estimated_minutes"]), "estimated"),
    ]))
    r.add(S.task_table("Planned tasks", ctx.tasks().filter(planned_date=day, is_unplanned=False),
                       empty="No tasks were planned for this day.", show_reason=True))
    r.add(S.incomplete_tasks(ctx, day, day))
    r.add(S.task_table("Unplanned work and interruptions", ctx.tasks().filter(is_unplanned=True, planned_date=day),
                       empty="No unplanned work recorded."))
    r.add(S.ticket_table("Tickets handled", handled, empty="No tickets handled."))
    r.add(S.maintenance_done(ctx))
    r.add(S.projects_progressed(ctx))
    r.add(time_table)
    r.add(time_chart)
    open_overdue = ctx.tasks().overdue(day + timedelta(days=1))
    outstanding = Section("Outstanding work at end of day", "metrics", metrics=[
        Metric("Overdue tasks", open_overdue.count()),
        Metric("Open tickets", ctx.tickets().open().count()),
        Metric("Open tickets past target", ctx.tickets().overdue().count()),
    ])
    r.add(outstanding)
    r.add(S.significant_incidents(ctx))
    nxt = ctx.tasks().filter(planned_date=day + timedelta(days=1)).exclude(status__in=Task.CLOSED_STATUSES)
    follow = ctx.tasks().filter(planned_date=day).exclude(follow_up_action="")
    actions = [f"{t.number} {t.title} (planned {t.planned_date:%d %b})" for t in nxt[:15]]
    actions += [f"Follow-up on {t.number}: {t.follow_up_action}" for t in follow[:10]]
    r.add(S.recommendations_section(ctx, actions))
    return _finish(r, ctx)


# ---------------------------------------------------------------------------
# Weekly
# ---------------------------------------------------------------------------
def build_weekly(ctx):
    from apps.tasks.models import Task

    r = _report(ctx, "Weekly IT report", f"{ctx.start:%d %b} – {ctx.end:%d %b %Y}")
    tf = S.task_figures(ctx, ctx.start, ctx.end)
    received, resolved, ticket_metrics = _ticket_metrics(ctx)
    records = ctx.time_records()
    summary, time_table, time_chart = S.time_sections(ctx, records)
    rate = f"{round(100 * tf['planned_done'] / tf['planned'])}%" if tf["planned"] else "—"
    r.add(Section("Summary", "metrics", metrics=[
        Metric("Tasks planned", tf["planned"]), Metric("Planned tasks completed", tf["planned_done"]),
        Metric("Completion rate", rate), Metric("Completed late", tf["late"]),
        Metric("Unplanned work items", tf["unplanned"]),
        *ticket_metrics[:4],
        Metric("Open tickets past target", ctx.tickets().overdue().count()),
        Metric("Time recorded", format_minutes(summary["total_minutes"]), "reported"),
        Metric("Unplanned share of time", f"{round(100 * summary['unplanned_minutes'] / summary['total_minutes'])}%"
               if summary["total_minutes"] else "—", "reported"),
    ]))
    days = [ctx.start + timedelta(days=i) for i in range((ctx.end - ctx.start).days + 1)]
    by_day = Section("Tasks planned vs completed by day", "table", columns=["Day", "Planned", "Completed", "Unplanned"])
    for d in days:
        f = S.task_figures(ctx, d, d)
        by_day.rows.append([d.strftime("%a %d %b"), f["planned"], f["planned_done"], f["unplanned"]])
    r.add(by_day)
    r.add(S.received_resolved_chart(ctx, [(d.strftime("%a %d"), d, d) for d in days]))
    r.add(S.significant_incidents(ctx, "Major incidents"))
    r.add(S.ticket_table("Outstanding and overdue tickets", ctx.tickets().open().filter(created_at__lte=ctx.end_dt),
                         order=("resolution_due_at",), empty="No open tickets."))
    comp, comp_table = S.compliance_section(ctx)
    r.add(comp)
    r.add(S.maintenance_done(ctx, "Preventive maintenance performed", kinds=["preventive", "inspection"]))
    r.add(S.maintenance_done(ctx, "Equipment repaired", kinds=["corrective"]))
    r.add(S.projects_progressed(ctx))
    r.add(time_table)
    r.add(time_chart)
    r.add(S.repeated_problems(ctx, min_category=2, min_asset=2))
    r.add(S.risks_section(ctx))
    next_start, next_end = ctx.end + timedelta(days=1), ctx.end + timedelta(days=7)
    priorities = ctx.tasks().filter(planned_date__range=(next_start, next_end)).exclude(
        status__in=Task.CLOSED_STATUSES).order_by("planned_date")
    actions = [f"{t.planned_date:%a %d %b}: {t.number} {t.title} ({t.get_priority_display()})" for t in priorities[:20]]
    actions += [f"Overdue: {t.number} {t.title}" for t in ctx.tasks().overdue(next_start)[:10]]
    rec = S.recommendations_section(ctx, actions)
    rec.title = "Next week's priorities and recommendations"
    r.add(rec)
    return _finish(r, ctx)


# ---------------------------------------------------------------------------
# Monthly
# ---------------------------------------------------------------------------
def _performance_summary(ctx, title):
    tf = S.task_figures(ctx, ctx.start, ctx.end)
    received, resolved, ticket_metrics = _ticket_metrics(ctx)
    summary, time_table, time_chart = S.time_sections(ctx)
    comp = S.compliance_metrics(ctx)
    proc_totals, maint_totals = S.expenditure(ctx)
    delivered = ctx.projects().filter(status="completed", completed_on__range=(ctx.start, ctx.end)).count()
    metrics = [
        Metric("Tasks completed", tf["completed_any"]),
        Metric("Planned tasks completed", f"{tf['planned_done']} of {tf['planned']}"),
        *ticket_metrics,
        Metric("Preventive maintenance compliance", f"{comp['rate']}%" if comp["rate"] is not None else "—"),
        Metric("Time recorded", format_minutes(summary["total_minutes"]), "reported"),
        Metric("Planned / unplanned time", f"{S.hours(summary['planned_minutes'])} h / {S.hours(summary['unplanned_minutes'])} h",
               "reported"),
        Metric("Projects delivered", delivered),
        Metric("Purchases delivered", S.money(proc_totals)),
        Metric("Maintenance & repair costs", S.money(maint_totals)),
    ]
    return Section(title, "metrics", metrics=metrics), time_table, time_chart, summary


def build_monthly(ctx):
    r = _report(ctx, "Monthly IT performance report", ctx.start.strftime("%B %Y") if ctx.start.day == 1 and
                (ctx.end + timedelta(days=1)).day == 1 and ctx.start.month == ctx.end.month else f"{ctx.start:%d %b} – {ctx.end:%d %b %Y}")
    summary_section, time_table, time_chart, time_summary = _performance_summary(ctx, "Summary")
    r.add(summary_section)
    r.add(S.resolution_stats_section(ctx))
    r.add(S.sla_section(ctx))
    r.add(S.department_section(ctx))
    r.add(S.category_section(ctx))
    r.add(S.significant_incidents(ctx, "Major incidents"))
    r.add(S.repeated_problems(ctx))
    comp, comp_table = S.compliance_section(ctx)
    r.add(comp)
    r.add(comp_table)
    r.add(S.maintenance_done(ctx, "Equipment repaired", kinds=["corrective"]))
    planned_unplanned = Section("Planned vs unplanned work", "chart", chart={
        "type": "doughnut", "labels": ["Planned", "Unplanned"],
        "datasets": [{"label": "Hours", "data": [S.hours(time_summary["planned_minutes"]),
                                                 S.hours(time_summary["unplanned_minutes"])]}]}) \
        if time_summary["total_minutes"] else None
    r.add(planned_unplanned)
    r.add(time_table)
    r.add(time_chart)
    if ctx.is_it:
        r.add(S.time_per_officer(ctx, ctx.time_records()))
    r.add(S.projects_delivered(ctx))
    r.add(S.projects_progressed(ctx))
    r.add(S.expenditure_section(ctx))
    r.add(S.savings_section(ctx))
    r.add(S.risks_section(ctx))
    r.add(S.recommendations_section(ctx))
    return _finish(r, ctx)


# ---------------------------------------------------------------------------
# Annual
# ---------------------------------------------------------------------------
def _months(start, end):
    out = []
    current = start.replace(day=1)
    while current <= end:
        nxt = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((current.strftime("%b %Y"), max(current, start), min(nxt - timedelta(days=1), end)))
        current = nxt
    return out


def build_annual(ctx):
    from apps.maintenance.services import compliance

    r = _report(ctx, "Annual IT report", f"{ctx.start:%d %b %Y} – {ctx.end:%d %b %Y}")
    summary_section, time_table, time_chart, _ts = _performance_summary(ctx, "Year at a glance")
    r.add(summary_section)
    months = _months(ctx.start, ctx.end)
    trend = Section("Monthly trends", "table", columns=["Month", "Tickets received", "Resolved", "Tasks completed",
                                                        "Hours recorded", "Maintenance on time", "Purchases delivered"])
    for label, a, b in months:
        sub = type(ctx)(kind="monthly", start=a, end=b, user=ctx.user, department_id=ctx.department_id,
                        task_category_id=ctx.task_category_id, ticket_category_id=ctx.ticket_category_id,
                        priority=ctx.priority, technician_id=ctx.technician_id, asset_id=ctx.asset_id,
                        project_id=ctx.project_id, activity_type=ctx.activity_type)
        tf = S.task_figures(sub, a, b)
        c = compliance(a, b, qs=sub.maintenance())
        proc_totals, _m = S.expenditure(sub)
        trend.rows.append([label, S.tickets_received(sub).count(), S.tickets_resolved(sub).count(), tf["completed_any"],
                           S.hours(sum(x.minutes for x in sub.time_records())),
                           f"{c['on_time']}/{c['completed'] + c['missed']}" if c["completed"] + c["missed"] else "—",
                           S.money(proc_totals)])
    r.add(trend)
    r.add(Section("Monthly workload trend", "chart", chart={
        "type": "line", "labels": [row[0] for row in trend.rows],
        "datasets": [{"label": "Tickets received", "data": [row[1] for row in trend.rows]},
                     {"label": "Tickets resolved", "data": [row[2] for row in trend.rows]},
                     {"label": "Tasks completed", "data": [row[3] for row in trend.rows]}]}))
    r.add(S.significant_incidents(ctx))
    r.add(S.projects_delivered(ctx))
    improvements = Section("Infrastructure improvements and equipment purchased", "table",
                           columns=["Delivered", "Reference", "Item", "Asset / project", "Actual cost"],
                           empty="No equipment purchases delivered.")
    for p in ctx.procurement().filter(status="delivered", delivery_date__range=(ctx.start, ctx.end),
                                      request_type__in=["purchase", "software"]).order_by("delivery_date"):
        improvements.rows.append([S._d(p.delivery_date), p.reference, p.item,
                                  (p.asset.tag if p.asset else "") + (" " + p.project.code if p.project else "") or "—",
                                  f"{p.actual_expenditure} {p.currency}"])
        improvements.links.append(p.get_absolute_url())
    r.add(improvements)
    r.add(S.lifecycle_section(ctx))
    challenges = S.category_section(ctx, min_count=3, title="Recurring challenges: most frequent request categories")
    challenges.empty = "No category had 3 or more requests."
    r.add(challenges)
    r.add(S.repeated_problems(ctx, min_category=5, min_asset=3))
    comp, comp_table = S.compliance_section(ctx)
    r.add(comp)
    r.add(comp_table)
    r.add(time_table)
    r.add(time_chart)
    r.add(S.expenditure_section(ctx))
    r.add(S.savings_section(ctx))
    r.add(S.risks_section(ctx))
    rec = S.recommendations_section(ctx)
    rec.title = "Recommendations for the coming year"
    r.add(rec)
    return _finish(r, ctx)


# ---------------------------------------------------------------------------
# Custom
# ---------------------------------------------------------------------------
def build_custom(ctx):
    from apps.diary.models import ActivityLog

    r = _report(ctx, "Custom IT report", f"{ctx.start:%d %b %Y} – {ctx.end:%d %b %Y}")
    tf = S.task_figures(ctx, ctx.start, ctx.end)
    received, resolved, ticket_metrics = _ticket_metrics(ctx)
    summary, time_table, time_chart = S.time_sections(ctx)
    r.add(Section("Summary", "metrics", metrics=[
        Metric("Tasks planned", tf["planned"]), Metric("Tasks completed", tf["completed_any"]),
        Metric("Unplanned work items", tf["unplanned"]), *ticket_metrics[:4],
        Metric("Time recorded", format_minutes(summary["total_minutes"]), "reported")]))
    tasks = ctx.tasks().filter(planned_date__range=(ctx.start, ctx.end))
    r.add(S.task_table("Tasks in period", tasks, show_reason=True))
    tickets = ctx.tickets().filter(created_at__range=(ctx.start_dt, ctx.end_dt))
    r.add(S.ticket_table("Tickets reported in period", tickets))
    r.add(time_table)
    r.add(time_chart)
    r.add(S.maintenance_done(ctx))
    if ctx.is_it:
        diary = ActivityLog.objects.current().filter(occurred_at__range=(ctx.start_dt, ctx.end_dt)).select_related("user")
        if ctx.technician_id:
            diary = diary.filter(user_id=ctx.technician_id)
        if ctx.activity_type:
            diary = diary.filter(activity_type=ctx.activity_type)
        if ctx.department_id:
            diary = diary.filter(department_id=ctx.department_id)
        if ctx.project_id:
            diary = diary.filter(project_id=ctx.project_id)
        if ctx.asset_id:
            diary = diary.filter(asset_id=ctx.asset_id)
        d = Section("Activity diary", "table", columns=["When", "Who", "Recorded", "Activity", "Entry", "Minutes"],
                    it_only=True, note="“Manual” entries are typed by IT staff; “System” entries are recorded automatically.")
        for a in diary.order_by("occurred_at")[:S.ROW_LIMIT + 1]:
            d.rows.append([S._d(a.occurred_at), S._name(a.user), "Manual" if a.source == "manual" else "System",
                           a.get_activity_type_display(), a.title, a.display_minutes or ""])
        r.add(S._limit(d))
    r.add(S.expenditure_section(ctx))
    r.add(S.recommendations_section(ctx))
    return _finish(r, ctx)


BUILDERS = {"daily": build_daily, "weekly": build_weekly, "monthly": build_monthly, "annual": build_annual,
            "custom": build_custom}


def default_period(kind, today=None):
    today = today or timezone.localdate()
    if kind == "daily":
        return today, today
    if kind == "weekly":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if kind == "monthly":
        first = today.replace(day=1)
        return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if kind == "annual":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    return today - timedelta(days=29), today
