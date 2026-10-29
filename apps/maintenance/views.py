import calendar
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.roles import IT_STAFF_ROLES
from apps.assets.models import Asset, AssetCategory
from apps.core.permissions import role_required
from apps.evidence import serving
from apps.tasks.forms import it_staff_queryset, parse_local_date
from apps.tasks.views import _apply_errors, _safe_next

from . import services
from .forms import CompleteForm, CorrectiveForm, EvidenceForm, PlanForm, ReasonForm, RescheduleForm
from .models import MaintenanceAttachment, MaintenancePlan, MaintenanceRecord

it_required = role_required(*IT_STAFF_ROLES)
R = MaintenanceRecord.Status
VIEWS = {"upcoming": "Upcoming", "overdue": "Overdue", "completed": "Completed", "missed": "Missed",
         "repairs": "Repairs", "all": "All"}


@it_required
def record_list(request):
    from apps.core.jobs import ensure_jobs_ran

    ensure_jobs_ran()
    today = timezone.localdate()
    view = request.GET.get("view", "upcoming")
    qs = MaintenanceRecord.objects.select_related("asset", "asset__category", "responsible", "performed_by", "plan")
    if view == "upcoming":
        qs = qs.filter(status=R.SCHEDULED, due_date__gte=today).order_by("due_date")
    elif view == "overdue":
        qs = qs.filter(status=R.SCHEDULED, due_date__lt=today).order_by("due_date")
    elif view == "completed":
        qs = qs.filter(status=R.COMPLETED).order_by("-completed_on")
    elif view == "missed":
        qs = qs.filter(status=R.MISSED).order_by("-due_date")
    elif view == "repairs":
        qs = qs.filter(kind=MaintenanceRecord.Kind.CORRECTIVE).order_by("-completed_on")
    else:
        view = "all"
        qs = qs.order_by("-due_date")
    f = {k: request.GET.get(k, "").strip() for k in ("q", "category", "responsible")}
    if f["q"]:
        qs = qs.filter(Q(title__icontains=f["q"]) | Q(asset__tag__icontains=f["q"]) | Q(asset__name__icontains=f["q"]))
    if f["category"].isdigit():
        qs = qs.filter(asset__category_id=f["category"])
    if f["responsible"].isdigit():
        qs = qs.filter(Q(responsible_id=f["responsible"]) | Q(performed_by_id=f["responsible"]))
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "maintenance/record_list.html", {
        "page_obj": page, "view": view, "views": VIEWS, "f": f, "today": today,
        "categories": AssetCategory.objects.all(), "officers": it_staff_queryset(),
        "plans": MaintenancePlan.objects.filter(is_active=True).select_related("asset", "responsible")
        if view == "upcoming" else None,
    })


@it_required
def calendar_view(request):
    today = timezone.localdate()
    try:
        year, month = int(request.GET.get("y", today.year)), int(request.GET.get("m", today.month))
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    start, end = weeks[0][0], weeks[-1][-1]
    records = MaintenanceRecord.objects.filter(
        Q(due_date__range=(start, end), status__in=[R.SCHEDULED, R.MISSED]) | Q(completed_on__range=(start, end))
    ).select_related("asset")
    by_day = {}
    for r in records:
        day = r.completed_on if r.status == R.COMPLETED else r.due_date
        by_day.setdefault(day, []).append(r)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return render(request, "maintenance/calendar.html", {
        "weeks": [[(d, by_day.get(d, [])) for d in week] for week in weeks], "first": first, "today": today,
        "prev": prev_month, "next": next_month,
    })


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
@it_required
def plan_edit(request, pk=None):
    plan = get_object_or_404(MaintenancePlan, pk=pk) if pk else None
    before = MaintenancePlan.objects.get(pk=pk) if pk else None
    if request.method == "POST":
        form = PlanForm(request.POST, instance=plan)
        if form.is_valid():
            try:
                if plan:
                    plan = services.update_plan(form.save(commit=False), before, request.user)
                else:
                    plan = services.create_plan(form.save(commit=False), request.user)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Maintenance plan saved. Next visit: {plan.next_due_date:%d %b %Y}.")
                return redirect(plan.asset)
    else:
        initial = {}
        asset = Asset.objects.filter(pk=request.GET.get("asset")).select_related("category").first() \
            if (request.GET.get("asset") or "").isdigit() else None
        if asset:
            days = asset.category.default_maintenance_days or 90
            initial = {"asset": asset.pk, "title": f"Preventive maintenance – {asset.name}",
                       "interval_value": days, "interval_unit": "days",
                       "next_due_date": timezone.localdate() + timedelta(days=min(days, 30)),
                       "responsible": request.user.pk}
        form = PlanForm(instance=plan, initial=initial or None)
    return render(request, "maintenance/plan_form.html", {"form": form, "plan": plan})


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@it_required
def record_detail(request, pk):
    record = get_object_or_404(MaintenanceRecord.objects.select_related(
        "asset", "plan", "responsible", "performed_by", "task", "ticket"), pk=pk)
    from apps.projects.models import Project

    return render(request, "maintenance/record_detail.html", {
        "record": record, "projects": Project.objects.exclude(status__in=["completed", "cancelled"]), "attachments": record.attachments.filter(is_deleted=False).select_related("uploaded_by"),
        "evidence_form": EvidenceForm(initial={"stage": "before"}),
    })


@it_required
def record_complete(request, pk):
    record = get_object_or_404(MaintenanceRecord.objects.select_related("asset", "plan"), pk=pk)
    checklist = record.plan.checklist_items if record.plan_id else []
    if record.status != R.SCHEDULED:
        messages.info(request, "This maintenance is not open.")
        return redirect(record)
    if request.method == "POST":
        form = CompleteForm(request.POST, request.FILES, checklist=checklist)
        if form.is_valid():
            d = form.cleaned_data
            try:
                services.complete_record(
                    record, request.user, completed_on=d["completed_on"], checklist_results=form.checklist_results(),
                    work_done=d["work_done"], inspection_results=d["inspection_results"],
                    problems_found=d["problems_found"], parts_used=d["parts_used"], cost=d["cost"],
                    currency=d["currency"], recommendations=d["recommendations"],
                    asset_condition=d["asset_condition"] or None, uploads=form.uploads)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, "Maintenance recorded." + (
                    f" Next visit scheduled for {record.plan.following(d['completed_on']):%d %b %Y}." if record.plan_id else ""))
                return redirect(record)
    else:
        form = CompleteForm(checklist=checklist)
    return render(request, "maintenance/complete_form.html", {"form": form, "record": record, "asset": record.asset,
                                                              "corrective": False})


@it_required
def corrective_create(request):
    from apps.helpdesk.models import Ticket

    asset = get_object_or_404(Asset, pk=request.GET.get("asset") or request.POST.get("asset_id") or 0)
    ticket = Ticket.objects.filter(pk=request.GET.get("ticket") or request.POST.get("ticket_id") or 0).first()
    if request.method == "POST":
        form = CorrectiveForm(request.POST, request.FILES)
        if form.is_valid():
            d = form.cleaned_data
            try:
                record = services.record_corrective(
                    asset, request.user, title=d["title"], work_done=d["work_done"], completed_on=d["completed_on"],
                    problems_found=d["problems_found"], parts_used=d["parts_used"], cost=d["cost"],
                    currency=d["currency"], recommendations=d["recommendations"], ticket=ticket,
                    asset_condition=d["asset_condition"] or None, asset_status=d["asset_status"] or None,
                    uploads=form.uploads)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Repair recorded on {asset.tag}.")
                return redirect(record)
    else:
        form = CorrectiveForm(initial={"title": f"Repair: {ticket.subject}"[:150] if ticket else ""})
    return render(request, "maintenance/complete_form.html", {"form": form, "asset": asset, "ticket": ticket,
                                                              "corrective": True})


def _record_action(request, pk, form_class, handler, heading, button, success, button_class="btn-primary", initial=None):
    record = get_object_or_404(MaintenanceRecord.objects.select_related("asset"), pk=pk)
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            try:
                handler(record, form.cleaned_data)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, success)
                return redirect(_safe_next(request, record.get_absolute_url()))
    else:
        form = form_class(initial=initial(record) if initial else None)
    return render(request, "maintenance/simple_action.html", {"form": form, "record": record, "heading": heading,
                                                              "button": button, "button_class": button_class})


@it_required
def record_reschedule(request, pk):
    return _record_action(request, pk, RescheduleForm,
                          lambda r, d: services.reschedule_record(r, request.user, d["new_date"], d["reason"]),
                          "Reschedule maintenance", "Reschedule", "Maintenance rescheduled.",
                          initial=lambda r: {"new_date": max(r.due_date, timezone.localdate()) + timedelta(days=7)})


@it_required
def record_cancel(request, pk):
    return _record_action(request, pk, ReasonForm, lambda r, d: services.cancel_record(r, request.user, d["reason"]),
                          "Cancel this visit", "Cancel visit", "Maintenance visit cancelled.", "btn-danger")


@it_required
@require_POST
def record_set_project(request, pk):
    from apps.core import audit
    from apps.core.models import AuditLog
    from apps.projects.models import Project

    record = get_object_or_404(MaintenanceRecord, pk=pk)
    value = request.POST.get("project", "")
    project = Project.objects.filter(pk=value).first() if value.isdigit() else None
    old = record.project
    record.project = project
    record.save(update_fields=["project", "updated_at"])
    audit.log(AuditLog.Action.UPDATE, record.asset, changes={"maintenance_project": [old.code if old else "",
                                                                                    project.code if project else ""]},
              message=f"Maintenance '{record.title}' project link changed")
    messages.success(request, "Project link saved.")
    return redirect(record)


@it_required
@require_POST
def record_attach(request, pk):
    record = get_object_or_404(MaintenanceRecord, pk=pk)
    form = EvidenceForm(request.POST, request.FILES)
    if form.is_valid() and form.uploads:
        services.add_attachments(record, form.uploads, request.user, form.cleaned_data["stage"],
                                 form.cleaned_data["description"])
        messages.success(request, f"{len(form.uploads)} file(s) added.")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        if form.is_valid():
            messages.error(request, "Choose at least one file.")
    return redirect(record)


@it_required
def attachment_view(request, pk):
    att = get_object_or_404(MaintenanceAttachment, pk=pk)
    if att.is_deleted:
        raise PermissionDenied("This file has been removed.")
    return serving.serve(request, att, download=request.GET.get("download") == "1")


# ---------------------------------------------------------------------------
# Compliance report
# ---------------------------------------------------------------------------
@it_required
def compliance_report(request):
    from apps.assets import services as asset_services

    today = timezone.localdate()
    end = parse_local_date(request.GET.get("end")) or today
    start = parse_local_date(request.GET.get("start")) or end.replace(day=1)
    if start > end:
        start, end = end, start
    result = services.compliance(start, end)
    by_category = {}
    for r in result["records"]:
        row = by_category.setdefault(r.asset.category.name, {"due": 0, "on_time": 0, "late": 0, "missed": 0, "open": 0})
        row["due"] += 1
        if r.status == R.COMPLETED:
            row["on_time" if r.on_time else "late"] += 1
        elif r.status == R.MISSED:
            row["missed"] += 1
        else:
            row["open"] += 1
    tz = timezone.get_current_timezone()
    since = timezone.make_aware(datetime.combine(start, time.min), tz)
    until = timezone.make_aware(datetime.combine(end, time.max), tz)
    failures = sorted(asset_services.failure_counts(since, until).select_related("category"),
                      key=lambda a: -(a.incidents + a.repairs))[:15]
    costs = MaintenanceRecord.objects.filter(status=R.COMPLETED, completed_on__range=(start, end), cost__isnull=False) \
        .values("currency").annotate(total=Sum("cost")).order_by("currency")
    return render(request, "maintenance/compliance.html", {
        "start": start, "end": end, "result": result, "by_category": sorted(by_category.items()),
        "failures": failures, "costs": costs,
    })
