"""Phase 3 dashboard metrics: requests, incidents, resolution performance against targets."""
from datetime import datetime, time
from statistics import median
from urllib.parse import urlencode

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.dashboard.registry import card_provider, chart_provider
from apps.tasks.models import Priority

from .models import Ticket

ROLES = (Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)
S = Ticket.Status


def _qs(filters):
    qs = Ticket.objects.all()
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    x = filters.extra
    if x.get("priority"):
        qs = qs.filter(priority=x["priority"])
    if x.get("officer"):
        qs = qs.filter(assigned_to_id=x["officer"])
    return qs


def _link(filters, **params):
    base = {"department": filters.department_id, "priority": filters.extra.get("priority"),
            "assigned": filters.extra.get("officer"), "queue": "all"}
    base.update(params)
    return reverse("helpdesk:list") + "?" + urlencode({k: v for k, v in base.items() if v not in (None, "")})


def _hours(minutes):
    return f"{minutes / 60:.1f} h" if minutes is not None else "—"


@card_provider("Helpdesk now", order=0, roles=ROLES)
def now_cards(filters):
    if filters.extra.get("role") != Role.MANAGER:
        from .services import ensure_jobs_ran

        ensure_jobs_ran()
    qs = _qs(filters)
    today = timezone.localdate()
    tz = timezone.get_current_timezone()
    day = (timezone.make_aware(datetime.combine(today, time.min), tz), timezone.make_aware(datetime.combine(today, time.max), tz))
    return [
        {"label": "New requests", "value": qs.filter(status=S.NEW).count(), "icon": "inbox", "tone": "primary",
         "url": _link(filters, queue="new"), "help": "Reported but not yet picked up by IT."},
        {"label": "Open incidents", "value": qs.open().filter(ticket_type=Ticket.Type.INCIDENT).count(),
         "icon": "exclamation-diamond", "tone": "warning", "url": _link(filters, queue="open", type="incident")},
        {"label": "Critical / high open", "value": qs.open().filter(priority__in=[Priority.CRITICAL, Priority.HIGH]).count(),
         "icon": "fire", "tone": "danger", "url": _link(filters, queue="critical")},
        {"label": "Past resolution target", "value": qs.overdue().count(), "icon": "alarm", "tone": "danger",
         "url": _link(filters, queue="overdue"), "help": "Open tickets past their resolution target (waiting time excluded)."},
        {"label": "Resolved today", "value": qs.filter(resolved_at__range=day).count(), "icon": "check2-circle",
         "tone": "success", "url": _link(filters, resolved=today.isoformat())},
        {"label": "Awaiting requester feedback", "value": qs.filter(status=S.RESOLVED).count(), "icon": "chat-square-dots",
         "tone": "secondary", "url": _link(filters, queue="resolved")},
    ]


def _period(filters):
    qs = _qs(filters)
    received = qs.filter(created_at__range=(filters.start_dt, filters.end_dt))
    resolved = qs.filter(resolved_at__range=(filters.start_dt, filters.end_dt)).exclude(resolved_at__isnull=True)
    return received, resolved


@card_provider("Helpdesk in selected period", order=3, roles=ROLES)
def period_cards(filters):
    received, resolved = _period(filters)
    resolved = list(resolved)
    durations = [t.resolution_minutes for t in resolved if t.resolution_minutes is not None]
    with_response = [t for t in received if t.response_due_at]
    resp_met = sum(1 for t in with_response if t.first_response_at and t.first_response_at <= t.response_due_at)
    res_met = sum(1 for t in resolved if t.resolution_due_at and t.resolved_at <= t.resolution_due_at)
    rated = [t.feedback_rating for t in resolved if t.feedback_rating]
    p = {"start": filters.start.isoformat(), "end": filters.end.isoformat()}
    return [
        {"label": "Tickets received", "value": received.count(), "icon": "box-arrow-in-down", "tone": "primary",
         "url": _link(filters, **p)},
        {"label": "Tickets resolved", "value": len(resolved), "icon": "check-all", "tone": "success"},
        {"label": "Average resolution time", "value": _hours(sum(durations) / len(durations) if durations else None),
         "icon": "hourglass-split", "tone": "secondary",
         "help": f"Median {_hours(median(durations) if durations else None)}. Report → resolved, excluding time waiting on others."},
        {"label": "Response target met", "value": f"{round(100 * resp_met / len(with_response))}%" if with_response else "—",
         "icon": "reply", "tone": "info", "help": "Tickets received in the period that got a first response in time."},
        {"label": "Resolution target met", "value": f"{round(100 * res_met / len(resolved))}%" if resolved else "—",
         "icon": "bullseye", "tone": "info", "help": "Tickets resolved in the period within their resolution target."},
        {"label": "Requester satisfaction", "value": f"{sum(rated) / len(rated):.1f} / 5" if rated else "—",
         "icon": "emoji-smile", "tone": "secondary", "help": f"From {len(rated)} rating(s). Optional, given by requesters."},
    ]


@chart_provider(order=0, roles=ROLES)
def ticket_charts(filters):
    received, resolved = _period(filters)
    rec = dict(received.annotate(d=TruncDate("created_at")).values("d").annotate(n=Count("id")).values_list("d", "n"))
    res = dict(resolved.annotate(d=TruncDate("resolved_at")).values("d").annotate(n=Count("id")).values_list("d", "n"))
    days = filters.days()
    charts = [{
        "id": "chart-received-resolved", "title": "Requests received vs resolved", "type": "line",
        "labels": [d.strftime("%d %b") for d in days],
        "datasets": [{"label": "Received", "data": [rec.get(d, 0) for d in days]},
                     {"label": "Resolved", "data": [res.get(d, 0) for d in days]}],
    }]
    by_dept = list(received.values("department__name").annotate(n=Count("id")).order_by("-n")[:12])
    if by_dept:
        charts.append({"id": "chart-ticket-dept", "title": "Tickets by department", "type": "bar", "horizontal": True,
                       "labels": [r["department__name"] or "No department" for r in by_dept],
                       "datasets": [{"label": "Tickets", "data": [r["n"] for r in by_dept]}]})
    incidents = received.filter(ticket_type=Ticket.Type.INCIDENT)
    rows = list(incidents.values("category__name", "priority").annotate(n=Count("id")))
    if rows:
        cats = sorted({r["category__name"] for r in rows})
        charts.append({
            "id": "chart-incident-cat", "title": "Incidents by category and priority", "type": "bar", "stacked": True,
            "horizontal": True, "labels": cats,
            "datasets": [{"label": label, "data": [sum(r["n"] for r in rows if r["category__name"] == c and r["priority"] == p)
                                                   for c in cats]}
                         for p, label in reversed(Priority.choices)],
        })
    return charts
