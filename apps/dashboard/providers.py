"""Phase 1 dashboard metrics: accounts, organisation and system activity."""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.urls import reverse

from apps.accounts.models import Department, Location
from apps.accounts.roles import Role
from apps.core.models import AuditLog

from .registry import card_provider, chart_provider

User = get_user_model()
ADMIN_ONLY = (Role.ADMIN,)


def _url(name, **params):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return reverse(name) + ("?" + urlencode(params) if params else "")


def _users(filters):
    qs = User.objects.filter(is_active=True)
    if filters.department_id:
        qs = qs.filter(profile__department_id=filters.department_id)
    return qs


@card_provider("Organisation", order=10)
def organisation_cards(filters):
    users = _users(filters)
    by_role = {r["profile__role"]: r["n"] for r in users.values("profile__role").annotate(n=Count("id"))}
    dept = filters.department_id or ""
    is_admin = filters.extra.get("role") == Role.ADMIN
    link = (lambda **p: _url("accounts:user_list", department=dept, **p)) if is_admin else (lambda **p: None)
    cards = [
        {"label": "Active user accounts", "value": users.count(), "icon": "people", "tone": "primary",
         "url": link(), "help": "Enabled accounts that can sign in."},
        {"label": "IT support officers", "value": by_role.get(Role.IT_OFFICER, 0), "icon": "person-gear",
         "tone": "info", "url": link(role=Role.IT_OFFICER)},
        {"label": "Managers", "value": by_role.get(Role.MANAGER, 0), "icon": "person-badge", "tone": "info",
         "url": link(role=Role.MANAGER)},
        {"label": "Employees", "value": by_role.get(Role.EMPLOYEE, 0), "icon": "person", "tone": "info",
         "url": link(role=Role.EMPLOYEE)},
    ]
    if not filters.department_id:
        cards += [
            {"label": "Active departments", "value": Department.objects.active().count(), "icon": "diagram-3",
             "tone": "secondary", "url": _url("accounts:department_list") if is_admin else None},
            {"label": "Active locations", "value": Location.objects.active().count(), "icon": "geo-alt",
             "tone": "secondary", "url": _url("accounts:location_list") if is_admin else None},
        ]
    return cards


@card_provider("System activity in period", order=20, roles=ADMIN_ONLY)
def activity_cards(filters):
    qs = AuditLog.objects.filter(timestamp__range=(filters.start_dt, filters.end_dt))
    counts = {r["action"]: r["n"] for r in qs.values("action").annotate(n=Count("id"))}
    period = {"start": filters.start.isoformat(), "end": filters.end.isoformat()}
    return [
        {"label": "Successful sign-ins", "value": counts.get(AuditLog.Action.LOGIN, 0), "icon": "box-arrow-in-right",
         "tone": "success", "url": _url("core:audit_log", action=AuditLog.Action.LOGIN, **period)},
        {"label": "Failed sign-ins", "value": counts.get(AuditLog.Action.LOGIN_FAILED, 0),
         "icon": "shield-exclamation", "tone": "warning",
         "url": _url("core:audit_log", action=AuditLog.Action.LOGIN_FAILED, **period),
         "help": "Wrong username/password attempts."},
        {"label": "Lockouts", "value": counts.get(AuditLog.Action.LOGIN_LOCKED, 0), "icon": "lock",
         "tone": "danger", "url": _url("core:audit_log", action=AuditLog.Action.LOGIN_LOCKED, **period)},
        {"label": "Access denied", "value": counts.get(AuditLog.Action.ACCESS_DENIED, 0), "icon": "slash-circle",
         "tone": "danger", "url": _url("core:audit_log", action=AuditLog.Action.ACCESS_DENIED, **period),
         "help": "Attempts to open pages the user's role does not permit."},
        {"label": "Configuration changes", "value": counts.get(AuditLog.Action.CONFIG_CHANGE, 0), "icon": "sliders",
         "tone": "secondary", "url": _url("core:audit_log", action=AuditLog.Action.CONFIG_CHANGE, **period)},
        {"label": "All audited events", "value": sum(counts.values()), "icon": "list-check", "tone": "secondary",
         "url": _url("core:audit_log", **period)},
    ]


@chart_provider(order=10, roles=ADMIN_ONLY)
def signin_chart(filters):
    rows = (
        AuditLog.objects.filter(timestamp__range=(filters.start_dt, filters.end_dt),
                                action__in=[AuditLog.Action.LOGIN, AuditLog.Action.LOGIN_FAILED])
        .annotate(day=TruncDate("timestamp")).values("day", "action").annotate(n=Count("id"))
    )
    data = {(r["day"], r["action"]): r["n"] for r in rows}
    days = filters.days()
    return [{
        "id": "chart-signins", "title": "Sign-in activity per day", "type": "line",
        "labels": [d.strftime("%d %b") for d in days],
        "datasets": [
            {"label": "Successful", "data": [data.get((d, AuditLog.Action.LOGIN), 0) for d in days]},
            {"label": "Failed", "data": [data.get((d, AuditLog.Action.LOGIN_FAILED), 0) for d in days]},
        ],
        "help": "Counted from the audit log in the hotel's timezone.",
    }]


@chart_provider(order=20)
def users_by_department_chart(filters):
    qs = Department.objects.active()
    if filters.department_id:
        qs = qs.filter(pk=filters.department_id)
    rows = list(qs.annotate(n=Count("members", filter=Q(members__user__is_active=True))).values_list("name", "n"))
    if not rows:
        return []
    return [{
        "id": "chart-departments", "title": "Active users by department", "type": "bar",
        "labels": [r[0] for r in rows],
        "datasets": [{"label": "Users", "data": [r[1] for r in rows]}],
    }]
