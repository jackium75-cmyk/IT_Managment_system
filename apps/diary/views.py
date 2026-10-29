from collections import OrderedDict
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.models import Department
from apps.accounts.roles import IT_STAFF_ROLES, Role, has_role
from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required
from apps.tasks.forms import it_staff_queryset, parse_local_date
from apps.tasks.models import Task

from .choices import ActivityType
from .forms import ManualEntryForm, VoidEntryForm
from .models import ActivityLog
from .timecalc import find_overlap, format_minutes, summarize, time_records

it_staff_required = role_required(*IT_STAFF_ROLES)
EDIT_FIELDS = ["occurred_at", "duration_minutes", "activity_type", "title", "description", "outcome",
               "department", "location"]


def _diary_owner(request):
    """Officers see their own diary; administrators may open any IT staff member's diary."""
    uid = request.GET.get("user")
    if uid and uid.isdigit() and int(uid) != request.user.pk:
        if not has_role(request.user, Role.ADMIN):
            audit.log(AuditLog.Action.ACCESS_DENIED, message="Attempted to open another user's diary")
            raise PermissionDenied("You can only view your own diary.")
        return get_object_or_404(it_staff_queryset(), pk=uid)
    return request.user


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end, time.max), tz))


@it_staff_required
def diary_list(request):
    owner = _diary_owner(request)
    today = timezone.localdate()
    end = parse_local_date(request.GET.get("end")) or today
    start = parse_local_date(request.GET.get("start")) or end - timedelta(days=6)
    if start > end:
        start, end = end, start
    f = {k: request.GET.get(k, "").strip() for k in ("q", "type", "source", "department", "task", "voided", "asset", "ticket", "project")}
    start_dt, end_dt = _bounds(start, end)

    qs = ActivityLog.objects.filter(user=owner, occurred_at__range=(start_dt, end_dt)).select_related(
        "task", "time_entry", "department", "asset")
    if f["voided"] != "1":
        qs = qs.current()
    if f["q"]:
        qs = qs.filter(Q(title__icontains=f["q"]) | Q(description__icontains=f["q"]) | Q(outcome__icontains=f["q"]))
    if f["type"] in ActivityType.values:
        qs = qs.filter(activity_type=f["type"])
    if f["source"] in ActivityLog.Source.values:
        qs = qs.filter(source=f["source"])
    if f["department"].isdigit():
        qs = qs.filter(department_id=f["department"])
    if f["task"]:
        qs = qs.filter(Q(task__number__iexact=f["task"]) | Q(task__number__icontains=f["task"]))
    if f["ticket"]:
        qs = qs.filter(ticket__reference__icontains=f["ticket"])
    if f["project"]:
        qs = qs.filter(Q(project__code__icontains=f["project"]) | Q(project__name__icontains=f["project"]))
    if f["asset"]:
        qs = qs.filter(Q(asset__tag__iexact=f["asset"]) | Q(asset__tag__icontains=f["asset"])
                       | Q(asset__name__icontains=f["asset"]))

    page = Paginator(qs.order_by("-occurred_at", "-id"), 50).get_page(request.GET.get("page"))
    days = OrderedDict()
    for entry in page:
        days.setdefault(timezone.localdate(entry.occurred_at), []).append(entry)

    # Counted time uses the single rule in timecalc (not the filtered list above).
    records = time_records(start_dt, end_dt, user=owner,
                           department_id=int(f["department"]) if f["department"].isdigit() else None,
                           activity_type=f["type"] if f["type"] in ActivityType.values else None)
    summary = summarize(records)
    planned = dict(
        Task.objects.filter(assigned_to=owner, planned_date__range=(start, end), is_unplanned=False)
        .values("planned_date").annotate(n=Count("id")).values_list("planned_date", "n"))
    done = dict(
        Task.objects.filter(assigned_to=owner, planned_date__range=(start, end), is_unplanned=False,
                            status=Task.Status.COMPLETED)
        .values("planned_date").annotate(n=Count("id")).values_list("planned_date", "n"))
    day_rows = [{
        "day": d, "entries": entries, "minutes": format_minutes(summary["by_day"].get(d, 0)),
        "planned": planned.get(d, 0), "done": done.get(d, 0),
    } for d, entries in days.items()]

    return render(request, "diary/diary_list.html", {
        "owner": owner, "is_own": owner == request.user, "page_obj": page, "day_rows": day_rows,
        "start": start, "end": end, "f": f, "types": ActivityType.choices, "sources": ActivityLog.Source.choices,
        "departments": Department.objects.active(), "summary": summary,
        "total": format_minutes(summary["total_minutes"]), "planned_time": format_minutes(summary["planned_minutes"]),
        "unplanned_time": format_minutes(summary["unplanned_minutes"]),
        "by_activity": [(label, format_minutes(m), m) for label, _k, m in summary["by_activity"]],
        "officers": it_staff_queryset() if has_role(request.user, Role.ADMIN) else None,
    })


def _get_entry(request, pk):
    entry = get_object_or_404(ActivityLog.objects.select_related("task", "time_entry", "user"), pk=pk)
    if entry.user_id != request.user.pk and not has_role(request.user, Role.ADMIN):
        audit.log(AuditLog.Action.ACCESS_DENIED, entry, message="Attempted to open another user's diary entry")
        raise PermissionDenied("This diary entry belongs to someone else.")
    return entry


@it_staff_required
def entry_detail(request, pk):
    entry = _get_entry(request, pk)
    history = AuditLog.objects.filter(content_type__model="activitylog", object_id=str(entry.pk))
    return render(request, "diary/entry_detail.html", {"entry": entry, "history": history,
                                                       "can_edit": entry.source == ActivityLog.Source.MANUAL
                                                       and entry.user_id == request.user.pk and not entry.is_voided})


def _check_overlap(form, user, exclude=None):
    d = form.cleaned_data
    start = d["occurred_at"]
    clash = find_overlap(user, start, start + timedelta(minutes=d["duration_minutes"]), exclude_activity=exclude)
    if clash:
        form.add_error("occurred_at", f"This overlaps {clash}. Time is never counted twice.")
        return False
    return True


@it_staff_required
def entry_create(request):
    if request.method == "POST":
        form = ManualEntryForm(request.POST)
        if form.is_valid() and _check_overlap(form, request.user):
            with transaction.atomic():
                entry = form.save(commit=False)
                entry.user = entry.created_by = request.user
                entry.source, entry.event = ActivityLog.Source.MANUAL, ActivityLog.Event.MANUAL
                entry.save()
                audit.log(AuditLog.Action.CREATE, entry, message=f"Manual diary entry ({entry.duration_minutes} min)")
            messages.success(request, "Diary entry added.")
            return redirect("diary:list")
    else:
        now = timezone.localtime().replace(second=0, microsecond=0)
        form = ManualEntryForm(initial={"occurred_at": now - timedelta(hours=1), "duration_minutes": 60,
                                        "activity_type": ActivityType.MEETING})
    return render(request, "diary/entry_form.html", {"form": form, "creating": True})


@it_staff_required
def entry_edit(request, pk):
    entry = _get_entry(request, pk)
    if entry.source != ActivityLog.Source.MANUAL or entry.is_voided or entry.user_id != request.user.pk:
        raise PermissionDenied("Only your own, current, manual entries can be edited. System entries are fixed.")
    before = ActivityLog.objects.get(pk=pk)
    if request.method == "POST":
        form = ManualEntryForm(request.POST, instance=entry)
        if form.is_valid() and _check_overlap(form, request.user, exclude=entry):
            with transaction.atomic():
                entry = form.save()
                changes = audit.diff_instances(before, entry, EDIT_FIELDS)
                if changes:
                    audit.log(AuditLog.Action.UPDATE, entry, changes=changes, message="Manual diary entry edited")
            messages.success(request, "Diary entry updated. The previous values are kept in the audit history.")
            return redirect("diary:detail", pk=entry.pk)
    else:
        form = ManualEntryForm(instance=entry)
    return render(request, "diary/entry_form.html", {"form": form, "entry": entry, "creating": False})


@it_staff_required
def entry_void(request, pk):
    entry = _get_entry(request, pk)
    if entry.source != ActivityLog.Source.MANUAL:
        raise PermissionDenied("System entries cannot be voided. Correct the underlying task or time entry instead.")
    if entry.is_voided:
        messages.info(request, "This entry is already void.")
        return redirect("diary:detail", pk=entry.pk)
    if request.method == "POST":
        form = VoidEntryForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                entry.is_voided = True
                entry.void_reason = form.cleaned_data["reason"]
                entry.voided_by, entry.voided_at = request.user, timezone.now()
                entry.save()
                audit.log(AuditLog.Action.DELETE, entry, message=f"Diary entry voided: {entry.void_reason}")
            messages.success(request, "Entry voided. It is kept for the record but no longer counted.")
            return redirect("diary:detail", pk=entry.pk)
    else:
        form = VoidEntryForm()
    return render(request, "diary/entry_void.html", {"form": form, "entry": entry})
