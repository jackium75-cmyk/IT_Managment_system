"""
Reusable report sections. Each function takes a ReportContext (plus a period
where relevant) and returns a Section built only from recorded data.
"""
from collections import Counter, defaultdict
from decimal import Decimal
from statistics import median

from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.diary.choices import ActivityType
from apps.diary.timecalc import format_minutes, summarize

from .structure import Metric, Section

ROW_LIMIT = 500
ACTIVITY_LABELS = dict(ActivityType.choices)


def _name(user):
    return (user.get_full_name() or user.username) if user else "—"


def _d(value, fmt="%d %b %Y"):
    if value is None:
        return "—"
    if hasattr(value, "hour"):
        return timezone.localtime(value).strftime(fmt + " %H:%M")
    return value.strftime(fmt)


def _limit(section):
    if len(section.rows) > ROW_LIMIT:
        section.note = (section.note + " " if section.note else "") + \
            f"Showing the first {ROW_LIMIT} of {len(section.rows)} rows; use the Excel export or filters for all."
        section.rows = section.rows[:ROW_LIMIT]
        section.links = section.links[:ROW_LIMIT]
    return section


def hours(minutes):
    return round((minutes or 0) / 60, 1)


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def fmt_duration(minutes):
    if minutes is None:
        return "—"
    minutes = int(round(minutes))
    if minutes >= 24 * 60:
        return f"{minutes / (24 * 60):.1f} days"
    return format_minutes(minutes)


def money(totals):
    return "; ".join(f"{amount:,.2f} {cur}" for cur, amount in sorted(totals.items())) or "None recorded"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def task_figures(ctx, start, end):
    from apps.tasks.models import Task

    planned = ctx.tasks().filter(planned_date__range=(start, end), is_unplanned=False)
    s_dt, e_dt = ctx.day_bounds(start)[0], ctx.day_bounds(end)[1]
    planned_done = planned.filter(status=Task.Status.COMPLETED)
    late = planned_done.annotate(d=TruncDate("completed_at")).filter(d__gt=F("planned_date")).count()
    completed_any = ctx.tasks().filter(status=Task.Status.COMPLETED, completed_at__range=(s_dt, e_dt))
    unplanned = ctx.tasks().filter(is_unplanned=True, planned_date__range=(start, end))
    moved = ctx.tasks().filter(schedule_changes__old_date__range=(start, end)).distinct()
    return {
        "planned": planned.count(), "planned_done": planned_done.count(), "late": late,
        "completed_any": completed_any.count(), "unplanned": unplanned.count(), "moved": moved.count(),
        "estimated_minutes": sum(t.estimated_minutes or 0 for t in planned),
    }


def task_table(title, qs, *, note="", empty="No tasks.", show_reason=False):
    s = Section(title, "table", note=note, empty=empty,
                columns=["Task", "Title", "Planned", "Assigned", "Status"] + (["Reason / follow-up"] if show_reason else []))
    for t in qs.order_by("planned_date", "scheduled_start", "id")[:ROW_LIMIT + 1]:
        row = [t.number, t.title, _d(t.planned_date), _name(t.assigned_to), t.get_status_display()]
        if show_reason:
            row.append(t.delay_reason or t.follow_up_action or "—")
        s.rows.append(row)
        s.links.append(t.get_absolute_url())
    return _limit(s)


def incomplete_tasks(ctx, day_start, day_end):
    """Planned tasks not completed in the period, plus tasks moved out of it, each with its recorded reason."""
    from apps.tasks.models import Task, TaskScheduleChange

    s = Section("Incomplete tasks and reasons", "table", columns=["Task", "Title", "Was planned", "Now", "Reason"],
                empty="All planned tasks were completed.", it_only=True)
    open_planned = ctx.tasks().filter(planned_date__range=(day_start, day_end), is_unplanned=False).exclude(
        status=Task.Status.COMPLETED)
    for t in open_planned:
        s.rows.append([t.number, t.title, _d(t.planned_date), t.get_status_display(),
                       t.delay_reason or "No reason recorded"])
        s.links.append(t.get_absolute_url())
    task_ids = ctx.tasks().values("pk")
    for c in TaskScheduleChange.objects.filter(old_date__range=(day_start, day_end), task__in=task_ids).exclude(
            new_date__range=(day_start, day_end)).select_related("task"):
        s.rows.append([c.task.number, c.task.title, _d(c.old_date), f"Moved to {_d(c.new_date)}", c.reason])
        s.links.append(c.task.get_absolute_url())
    return _limit(s)


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------
def tickets_received(ctx):
    return ctx.tickets().filter(created_at__range=(ctx.start_dt, ctx.end_dt))


def tickets_resolved(ctx):
    return ctx.tickets().filter(resolved_at__range=(ctx.start_dt, ctx.end_dt))


def tickets_handled(ctx):
    """Tickets IT worked on in the period: a status change, IT reply/note or time entry by IT staff."""
    from apps.helpdesk.models import TicketComment, TicketStatusHistory, TicketTimeEntry
    from apps.tasks.forms import it_staff_queryset

    staff = it_staff_queryset().values("pk")
    rng = (ctx.start_dt, ctx.end_dt)
    ids = set(TicketStatusHistory.objects.filter(changed_at__range=rng, changed_by__in=staff).values_list("ticket_id", flat=True))
    ids |= set(TicketComment.objects.filter(created_at__range=rng, author__in=staff).values_list("ticket_id", flat=True))
    ids |= set(TicketTimeEntry.objects.filter(started_at__range=rng, is_voided=False).values_list("ticket_id", flat=True))
    return ctx.tickets().filter(pk__in=ids)


def ticket_table(title, qs, *, note="", empty="No tickets.", order=("-created_at",)):
    s = Section(title, "table", note=note, empty=empty,
                columns=["Ticket", "Subject", "Department", "Priority", "Status", "Reported", "Resolved"])
    for t in qs.order_by(*order)[:ROW_LIMIT + 1]:
        s.rows.append([t.reference, t.subject, str(t.department or "—"), t.get_priority_display(), t.get_status_display(),
                       _d(t.created_at), _d(t.resolved_at)])
        s.links.append(t.get_absolute_url())
    return _limit(s)


def resolution_minutes(tickets):
    return [t.resolution_minutes for t in tickets if t.resolution_minutes is not None]


def resolution_stats_section(ctx):
    from apps.helpdesk.services import targets_for
    from apps.tasks.models import Priority

    resolved = list(tickets_resolved(ctx))
    s = Section("Resolution time by priority", "table",
                columns=["Priority", "Resolved", "Average", "Median", "90% within", "Target"],
                note="Measured from report to resolution, excluding time waiting on the requester, vendors, parts or approvals.")
    for value, label in reversed(Priority.choices):
        mins = resolution_minutes([t for t in resolved if t.priority == value])
        s.rows.append([label, len(mins), fmt_duration(sum(mins) / len(mins) if mins else None),
                       fmt_duration(median(mins) if mins else None), fmt_duration(percentile(mins, 0.9)),
                       fmt_duration(targets_for(value)[1])])
    mins = resolution_minutes(resolved)
    s.rows.append(["All", len(mins), fmt_duration(sum(mins) / len(mins) if mins else None),
                   fmt_duration(median(mins) if mins else None), fmt_duration(percentile(mins, 0.9)), ""])
    return s


def sla_section(ctx):
    from apps.tasks.models import Priority

    received = list(tickets_received(ctx))
    resolved = list(tickets_resolved(ctx))
    s = Section("Performance against service targets", "table",
                columns=["Priority", "Received", "First response on time", "Resolved", "Resolved within target"],
                note="Response measured on tickets received in the period; resolution on tickets resolved in the period. "
                     "Time spent waiting on others is excluded from the resolution target.")

    def pct(n, d):
        return f"{round(100 * n / d)}% ({n}/{d})" if d else "—"

    for value, label in reversed(Priority.choices):
        rec = [t for t in received if t.priority == value and t.response_due_at]
        res = [t for t in resolved if t.priority == value and t.resolution_due_at]
        s.rows.append([label, len(rec), pct(sum(1 for t in rec if t.first_response_at and t.first_response_at <= t.response_due_at), len(rec)),
                       len(res), pct(sum(1 for t in res if t.resolved_at <= t.resolution_due_at), len(res))])
    return s


def sla_rates(ctx):
    received = [t for t in tickets_received(ctx) if t.response_due_at]
    resolved = [t for t in tickets_resolved(ctx) if t.resolution_due_at]
    r1 = round(100 * sum(1 for t in received if t.first_response_at and t.first_response_at <= t.response_due_at)
               / len(received)) if received else None
    r2 = round(100 * sum(1 for t in resolved if t.resolved_at <= t.resolution_due_at) / len(resolved)) if resolved else None
    return r1, r2


def department_section(ctx):
    received = tickets_received(ctx)
    resolved = list(tickets_resolved(ctx))
    rec = dict(received.values_list("department__name").annotate(n=Count("id")).values_list("department__name", "n"))
    names = sorted(set(rec) | {t.department.name if t.department else None for t in resolved}, key=lambda x: x or "")
    s = Section("Requests by department", "table",
                columns=["Department", "Received", "Incidents", "Service requests", "Resolved", "Median resolution"])
    inc = dict(received.filter(ticket_type="incident").values_list("department__name").annotate(n=Count("id"))
               .values_list("department__name", "n"))
    for name in names:
        dept_res = [t for t in resolved if (t.department.name if t.department else None) == name]
        mins = resolution_minutes(dept_res)
        s.rows.append([name or "No department", rec.get(name, 0), inc.get(name, 0), rec.get(name, 0) - inc.get(name, 0),
                       len(dept_res), fmt_duration(median(mins) if mins else None)])
    s.rows.sort(key=lambda r: -r[1])
    return s


def category_section(ctx, min_count=1, title="Requests by category"):
    received = tickets_received(ctx)
    rows = received.values("category__name", "subcategory__name").annotate(
        n=Count("id"), incidents=Count("id", filter=Q(ticket_type="incident"))).order_by("-n")
    s = Section(title, "table", columns=["Category", "Subcategory", "Tickets", "Incidents"])
    for r in rows:
        if r["n"] >= min_count:
            s.rows.append([r["category__name"], r["subcategory__name"] or "—", r["n"], r["incidents"]])
    return s


def significant_incidents(ctx, title="Significant incidents"):
    from apps.tasks.models import Priority

    qs = ctx.tickets().filter(ticket_type="incident", priority__in=[Priority.CRITICAL, Priority.HIGH]).filter(
        Q(created_at__range=(ctx.start_dt, ctx.end_dt)) | Q(resolved_at__range=(ctx.start_dt, ctx.end_dt))
        | Q(created_at__lte=ctx.end_dt, resolved_at__isnull=True))
    s = ticket_table(title, qs, empty="No critical or high-priority incidents.",
                     note="Critical and high-priority incidents reported, resolved or still open in the period.")
    s.columns.append("Resolution")
    for i, t in enumerate(qs.order_by("-created_at")[:ROW_LIMIT]):
        s.rows[i].append((t.resolution_details or "")[:200] or "—")
    return s


def repeated_problems(ctx, min_category=3, min_asset=2):
    from apps.assets.services import failure_counts

    s = Section("Repeated problems", "table", columns=["Type", "Item", "Incidents", "Repairs"],
                note=f"Ticket categories with {min_category}+ incidents and assets with {min_asset}+ failures in the period.",
                empty="No repeated problems in this period.")
    for r in tickets_received(ctx).filter(ticket_type="incident").values("category__name", "subcategory__name").annotate(
            n=Count("id")).filter(n__gte=min_category).order_by("-n"):
        s.rows.append(["Category", f"{r['category__name']}{' › ' + r['subcategory__name'] if r['subcategory__name'] else ''}",
                       r["n"], "—"])
        s.links.append("")
    for a in sorted(failure_counts(ctx.start_dt, ctx.end_dt, qs=ctx.assets()), key=lambda a: -(a.incidents + a.repairs)):
        if a.incidents + a.repairs >= min_asset:
            s.rows.append(["Asset", f"{a.tag} {a.name}", a.incidents, a.repairs])
            s.links.append(a.get_absolute_url())
    return s


# ---------------------------------------------------------------------------
# Maintenance, assets
# ---------------------------------------------------------------------------
def maintenance_done(ctx, title="Maintenance performed", kinds=None):
    qs = ctx.maintenance().filter(status="completed", completed_on__range=(ctx.start, ctx.end))
    if kinds:
        qs = qs.filter(kind__in=kinds)
    s = Section(title, "table", columns=["Date", "Asset", "Work", "Type", "Done by", "Issues found", "Cost"])
    for r in qs.order_by("completed_on"):
        s.rows.append([_d(r.completed_on), f"{r.asset.tag} {r.asset.name}", r.title, r.get_kind_display(),
                       _name(r.performed_by), len(r.issues) or (r.problems_found[:80] if r.problems_found else 0),
                       f"{r.cost} {r.currency}" if r.cost is not None else "—"])
        s.links.append(r.get_absolute_url())
    return _limit(s)


def compliance_metrics(ctx):
    from apps.maintenance.services import compliance

    return compliance(ctx.start, ctx.end, qs=ctx.maintenance())


def compliance_section(ctx):
    c = compliance_metrics(ctx)
    s = Section("Preventive maintenance compliance", "metrics", note=(
        "Visits are counted in the period of their original due date. On time = completed by that date. "
        "Compliance = on time ÷ (completed + missed); visits still open are not counted yet."))
    s.metrics = [Metric("Visits due", c["due"]), Metric("Completed on time", c["on_time"]),
                 Metric("Completed late", c["late"]), Metric("Missed", c["missed"]), Metric("Still open", c["open"]),
                 Metric("Compliance", f"{c['rate']}%" if c["rate"] is not None else "—")]
    by_cat = defaultdict(lambda: [0, 0, 0, 0, 0])
    for r in c["records"]:
        row = by_cat[r.asset.category.name]
        row[0] += 1
        if r.status == "completed":
            row[1 if r.on_time else 2] += 1
        elif r.status == "missed":
            row[3] += 1
        else:
            row[4] += 1
    table = Section("Maintenance compliance by equipment category", "table",
                    columns=["Category", "Due", "On time", "Late", "Missed", "Open"],
                    rows=[[k, *v] for k, v in sorted(by_cat.items())], empty="No preventive maintenance due.")
    return s, table


def lifecycle_section(ctx):
    from apps.assets.models import Asset

    s = Section("Equipment lifecycle needs", "table", columns=["Asset", "Category", "Age (years)", "Condition", "Why"],
                empty="No equipment flagged for replacement.")
    for a in ctx.assets().in_service():
        reasons = []
        if a.replacement in (Asset.Replacement.RECOMMENDED, Asset.Replacement.URGENT):
            reasons.append(a.get_replacement_display() + (f": {a.replacement_reason}" if a.replacement_reason else ""))
        if a.beyond_expected_life:
            reasons.append(f"past expected life of {a.category.expected_life_years} years")
        if a.condition in ("poor", "broken"):
            reasons.append(f"condition {a.get_condition_display().lower()}")
        if reasons:
            s.rows.append([f"{a.tag} {a.name}", a.category.name, a.age_years if a.age_years is not None else "—",
                           a.get_condition_display(), "; ".join(reasons)])
            s.links.append(a.get_absolute_url())
    return _limit(s)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def time_sections(ctx, records=None):
    records = records if records is not None else ctx.time_records()
    s = summarize(records)
    table = Section("Time by activity", "table", columns=["Activity", "Hours", "Share"],
                    note="Time recorded by IT staff (timers, entries made afterwards and manual diary entries). "
                         "Each minute is counted once: time on a task linked to a ticket is counted on the ticket.",
                    empty="No time recorded.")
    total = s["total_minutes"] or 1
    for label, _key, minutes in s["by_activity"]:
        table.rows.append([label, hours(minutes), f"{round(100 * minutes / total)}%"])
    chart = None
    if s["by_activity"]:
        chart = Section("Hours by activity", "chart", chart={
            "type": "bar", "horizontal": True, "labels": [label for label, _k, _m in s["by_activity"]],
            "datasets": [{"label": "Hours", "data": [hours(m) for _l, _k, m in s["by_activity"]]}]})
    return s, table, chart


def time_per_officer(ctx, records):
    from django.contrib.auth import get_user_model

    by_user = defaultdict(lambda: {"total": 0, "unplanned": 0, "support": 0})
    for r in records:
        u = by_user[r.user_id]
        u["total"] += r.minutes
        if r.is_unplanned:
            u["unplanned"] += r.minutes
        if r.activity_type == ActivityType.SUPPORT:
            u["support"] += r.minutes
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=by_user)}
    s = Section("Recorded time per IT staff member", "table", it_only=True,
                columns=["Name", "Hours recorded", "Unplanned share", "Support share"],
                note="Listed alphabetically. Recorded time depends on how consistently people log their work, so it is "
                     "not a measure of performance on its own.")
    for uid, v in sorted(by_user.items(), key=lambda kv: _name(users.get(kv[0])).lower()):
        s.rows.append([_name(users.get(uid)), hours(v["total"]),
                       f"{round(100 * v['unplanned'] / v['total'])}%" if v["total"] else "—",
                       f"{round(100 * v['support'] / v['total'])}%" if v["total"] else "—"])
    return s


# ---------------------------------------------------------------------------
# Projects, money
# ---------------------------------------------------------------------------
def projects_progressed(ctx):
    from apps.projects.models import ProjectMilestone, ProjectUpdate

    projects = ctx.projects()
    s = Section("Projects progressed", "table", columns=["Project", "Status", "Progress", "What happened in the period"],
                empty="No project activity recorded.")
    for p in projects:
        updates = ProjectUpdate.objects.filter(project=p, created_at__range=(ctx.start_dt, ctx.end_dt), is_system=False)
        done = ProjectMilestone.objects.filter(project=p, completed_on__range=(ctx.start, ctx.end))
        if not updates.exists() and not done.exists() and not (p.completed_on and ctx.start <= p.completed_on <= ctx.end):
            continue
        events = [f"Milestone done: {m.title}" for m in done] + [u.body[:160] for u in updates.order_by("created_at")]
        if p.completed_on and ctx.start <= p.completed_on <= ctx.end:
            events.insert(0, "Project completed")
        s.rows.append([f"{p.code} {p.name}", p.get_status_display(),
                       f"{p.progress}%" if p.progress is not None else "—", " · ".join(events)])
        s.links.append(p.get_absolute_url())
    return s


def projects_delivered(ctx):
    from apps.projects.services import costs

    qs = ctx.projects().filter(status="completed", completed_on__range=(ctx.start, ctx.end))
    s = Section("Projects and improvements delivered", "table",
                columns=["Project", "Completed", "Outcome", "Benefits", "Benefits basis", "Actual cost"],
                empty="No projects completed in this period.")
    for p in qs:
        actual = {cur: c["actual"] for cur, c in costs(p).items() if c["actual"]}
        s.rows.append([f"{p.code} {p.name}", _d(p.completed_on), p.final_outcome[:200], (p.benefits or "—")[:200],
                       "Confirmed by " + _name(p.benefits_verified_by) if p.benefits_verified_by else "Reported by IT",
                       money(actual)])
        s.links.append(p.get_absolute_url())
    return s


def expenditure(ctx):
    from apps.procurement.services import spend_by_currency

    proc = ctx.procurement().filter(delivery_date__range=(ctx.start, ctx.end))
    maint = defaultdict(Decimal)
    for r in ctx.maintenance().filter(status="completed", completed_on__range=(ctx.start, ctx.end), cost__isnull=False):
        maint[r.currency or "?"] += r.cost
    return spend_by_currency(proc), dict(maint)


def expenditure_section(ctx):
    proc_totals, maint_totals = expenditure(ctx)
    s = Section("Expenditure recorded", "table",
                columns=["Date", "Reference", "Item / service", "Type", "Asset / project", "Approved", "Actual"],
                note=f"Purchases delivered: {money(proc_totals)}. Maintenance and repair costs: {money(maint_totals)}. "
                     "Amounts are shown per currency as recorded; this is not an accounting statement.",
                empty="No delivered purchases in this period.")
    for r in ctx.procurement().filter(delivery_date__range=(ctx.start, ctx.end), status="delivered").order_by("delivery_date"):
        s.rows.append([_d(r.delivery_date), r.reference, r.item, r.get_request_type_display(),
                       (r.asset.tag if r.asset else "") + (" " + r.project.code if r.project else "") or "—",
                       f"{r.approved_cost} {r.currency}" if r.approved_cost is not None else "—",
                       f"{r.actual_expenditure} {r.currency}"])
        s.links.append(r.get_absolute_url())
    return _limit(s)


def savings_section(ctx):
    s = Section("Documented cost savings", "table", columns=["Source", "Amount", "Evidence", "Basis"],
                note="Only savings with recorded evidence are listed. No savings are estimated or inferred by the system.",
                empty="No documented savings in this period.")
    for p in ctx.projects().filter(documented_savings__isnull=False, completed_on__range=(ctx.start, ctx.end)):
        s.rows.append([f"{p.code} {p.name}", f"{p.documented_savings} {p.currency}", p.savings_evidence,
                       "Confirmed by " + _name(p.benefits_verified_by) if p.benefits_verified_by else "Reported by IT"])
        s.links.append(p.get_absolute_url())
    for r in ctx.procurement().filter(delivery_date__range=(ctx.start, ctx.end)).exclude(cost_saving_note=""):
        s.rows.append([f"{r.reference} {r.item}", "see note", r.cost_saving_note, "Reported by IT"])
        s.links.append(r.get_absolute_url())
    return s


# ---------------------------------------------------------------------------
# Risks, observations, recommendations
# ---------------------------------------------------------------------------
def observations(ctx):
    """Neutral facts derived from the data that may need management attention."""
    from apps.assets.services import needing_attention
    from apps.helpdesk.models import Ticket
    from apps.tasks.models import Task

    out = []
    attention = needing_attention(ctx.assets())
    if attention:
        out.append(f"{len(attention)} asset(s) need attention (e.g. {', '.join(a.tag for a, _r in attention[:5])}).")
    overdue_tickets = ctx.tickets().overdue().count()
    if overdue_tickets:
        out.append(f"{overdue_tickets} open ticket(s) are past their resolution target.")
    waiting = ctx.tickets().filter(status=Ticket.Status.WAITING).values("waiting_on").annotate(n=Count("id"))
    for w in waiting:
        out.append(f"{w['n']} ticket(s) waiting on {dict(Ticket.WaitingOn.choices).get(w['waiting_on'], 'others').lower()}.")
    c = compliance_metrics(ctx)
    if c["missed"]:
        out.append(f"{c['missed']} preventive maintenance visit(s) were missed.")
    top = Counter(tickets_received(ctx).filter(ticket_type="incident").values_list("category__name", flat=True)).most_common(1)
    if top and top[0][1] >= 3:
        out.append(f"Most incidents were in “{top[0][0]}” ({top[0][1]}).")
    if ctx.is_it:
        overdue_tasks = ctx.tasks().overdue().count()
        if overdue_tasks:
            out.append(f"{overdue_tasks} task(s) are overdue.")
        blocked = ctx.tasks().filter(status__in=[Task.Status.BLOCKED, Task.Status.WAITING_PARTS, Task.Status.WAITING_INFO]).count()
        if blocked:
            out.append(f"{blocked} task(s) are blocked or waiting for information/parts.")
    for p in ctx.projects().active():
        if p.is_overdue:
            out.append(f"Project {p.code} is past its target date ({_d(p.target_date)}).")
    return out


def risks_section(ctx):
    s = Section("Risks and blockers", "table", columns=["Source", "Risk / blocker"], empty="No open risks recorded.")
    for p in ctx.projects().active():
        if p.risks:
            s.rows.append([f"{p.code} {p.name}", p.risks[:300]])
            s.links.append(p.get_absolute_url())
        if p.blockers:
            s.rows.append([f"{p.code} {p.name} (blocker)", p.blockers[:300]])
            s.links.append(p.get_absolute_url())
    for text in observations(ctx):
        s.rows.append(["System observation", text])
        s.links.append("")
    return s


def recommendations_section(ctx, next_actions=None):
    paragraphs = []
    if ctx.notes.strip():
        paragraphs.append(("IT officer's commentary and recommendations (entered when the report was generated):",
                           ctx.notes.strip()))
    obs = observations(ctx)
    if obs:
        paragraphs.append(("Observations from recorded data:", "\n".join(f"• {o}" for o in obs)))
    if next_actions:
        paragraphs.append(("Next actions already planned:", "\n".join(f"• {a}" for a in next_actions)))
    return Section("Recommendations and next actions", "text", paragraphs=paragraphs,
                   empty="No commentary entered. Add your recommendations in the report form to include them.")


def received_resolved_chart(ctx, buckets):
    """buckets: list of (label, start_date, end_date)."""
    received = tickets_received(ctx).values_list("created_at", flat=True)
    resolved = tickets_resolved(ctx).values_list("resolved_at", flat=True)
    rec, res = Counter(), Counter()
    for dt in received:
        rec[timezone.localdate(dt)] += 1
    for dt in resolved:
        res[timezone.localdate(dt)] += 1

    def total(counter, a, b):
        return sum(v for k, v in counter.items() if a <= k <= b)

    return Section("Requests received vs resolved", "chart", chart={
        "type": "bar", "labels": [b[0] for b in buckets],
        "datasets": [{"label": "Received", "data": [total(rec, b[1], b[2]) for b in buckets]},
                     {"label": "Resolved", "data": [total(res, b[1], b[2]) for b in buckets]}]})
