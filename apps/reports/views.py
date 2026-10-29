from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.roles import Role
from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required

from . import analytics, builders, exporters
from .context import ReportContext
from .forms import ReportForm

viewer_required = role_required(Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}
MAX_DAYS = {"daily": 1, "weekly": 31, "monthly": 93, "annual": 400, "custom": 400}


def _context_from(form, user, kind):
    d = form.cleaned_data
    start, end = d.get("start"), d.get("end")
    default_start, default_end = builders.default_period(kind)
    if kind == "daily":
        start = end = start or end or default_start
    start, end = start or default_start, end or default_end
    if start > end:
        start, end = end, start
    if (end - start).days + 1 > MAX_DAYS[kind]:
        end = start + timedelta(days=MAX_DAYS[kind] - 1)
    return ReportContext(
        kind=kind, start=start, end=end, user=user,
        department_id=d["department"].pk if d.get("department") else None,
        task_category_id=d["task_category"].pk if d.get("task_category") else None,
        ticket_category_id=d["ticket_category"].pk if d.get("ticket_category") else None,
        priority=d.get("priority") or None, status=d.get("status") or None,
        technician_id=d["technician"].pk if d.get("technician") else None,
        asset_id=d["asset"].pk if d.get("asset") else None,
        project_id=d["project"].pk if d.get("project") else None,
        activity_type=d.get("activity_type") or None, notes=d.get("notes") or "",
    )


@viewer_required
def centre(request):
    manager = request.user.profile.role == Role.MANAGER and not request.user.is_superuser
    if "kind" not in request.GET:
        return render(request, "reports/centre.html", {"form": ReportForm(user=request.user, manager=manager),
                                                       "kinds": _kind_cards()})
    form = ReportForm(request.GET, user=request.user, manager=manager)
    if not form.is_valid():
        return render(request, "reports/centre.html", {"form": form, "kinds": _kind_cards()})
    kind = form.cleaned_data["kind"]
    ctx = _context_from(form, request.user, kind)
    report = builders.BUILDERS[kind](ctx)
    fmt = request.GET.get("format", "html")
    audit.log(AuditLog.Action.REPORT, message=f"{report.title} ({report.period_label}) generated as {fmt.upper()}; "
                                              f"filters: {report.filters}"[:500])
    if fmt in CONTENT_TYPES:
        data = {"pdf": exporters.to_pdf, "xlsx": exporters.to_xlsx, "csv": exporters.to_csv}[fmt](report)
        resp = HttpResponse(data, content_type=CONTENT_TYPES[fmt])
        name = f"{slugify(report.title)}-{ctx.start:%Y%m%d}" + (f"-{ctx.end:%Y%m%d}" if ctx.end != ctx.start else "")
        resp["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
        resp["Cache-Control"] = "private, no-store"
        return resp
    if fmt != "html":
        messages.error(request, "Unknown export format.")
    for i, s in enumerate(report.sections):  # presentation helpers for the HTML template
        s.dom_id = f"report-chart-{i}"
        s.pairs = [(row, s.links[j] if j < len(s.links) else "") for j, row in enumerate(s.rows)]
    return render(request, "reports/report.html", {"report": report, "form": form, "query": request.GET.urlencode(),
                                                   "ctx": ctx})


def _kind_cards():
    return [
        ("daily", "calendar-day", "Daily IT activity", "Planned vs done, incomplete tasks with reasons, unplanned work, "
                                                       "tickets handled, maintenance, time, next actions."),
        ("weekly", "calendar-week", "Weekly IT report", "Planned vs completed, requests received and resolved, major "
                                                        "incidents, maintenance, repairs, repeated problems, next week."),
        ("monthly", "calendar3", "Monthly performance", "Resolution times, service-target performance, departments, "
                                                        "failures, compliance, projects, expenditure, savings, risks."),
        ("annual", "calendar-range", "Annual IT report", "Monthly trends, major incidents, projects, improvements, "
                                                         "equipment lifecycle, recurring challenges, spend."),
        ("custom", "funnel", "Custom report", "Any period with filters for department, categories, priority, status, "
                                              "technician, asset, project and activity type."),
    ]


@viewer_required
def analytics_view(request):
    manager = request.user.profile.role == Role.MANAGER and not request.user.is_superuser
    today = timezone.localdate()
    start, end = today - timedelta(days=89), today
    form = ReportForm(request.GET or None, user=request.user, manager=manager,
                      initial={"kind": "custom", "start": start, "end": end})
    department = technician = None
    if request.GET and form.is_valid():
        start = form.cleaned_data.get("start") or start
        end = form.cleaned_data.get("end") or end
        department = form.cleaned_data.get("department")
        technician = form.cleaned_data.get("technician")
    if (end - start).days + 1 > MAX_DAYS["custom"]:
        start = end - timedelta(days=MAX_DAYS["custom"] - 1)
    ctx = ReportContext(kind="analytics", start=start, end=end, user=request.user,
                        department_id=department.pk if department else None,
                        technician_id=technician.pk if technician else None)
    data = analytics.build(ctx)
    audit.log(AuditLog.Action.REPORT, message=f"Workload analytics viewed {ctx.start} – {ctx.end}")
    trend = data["trend"]
    trend_chart = {"type": "line", "labels": [r["label"] for r in trend], "datasets": [
        {"label": "Received", "data": [r["received"] for r in trend]},
        {"label": "Resolved", "data": [r["resolved"] for r in trend]},
        {"label": "Open at week end", "data": [r["backlog"] for r in trend]}]}
    return render(request, "reports/analytics.html", {"d": data, "ctx": ctx, "form": form, "trend_chart": trend_chart})
