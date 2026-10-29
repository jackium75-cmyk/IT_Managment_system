from datetime import datetime, time, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.models import Department
from apps.accounts.roles import IT_STAFF_ROLES, Role
from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required
from apps.diary.timecalc import format_minutes

from . import recurrence, services
from .forms import (
    BulkRescheduleForm, CategoryForm, CommentForm, CompleteForm, LinkTicketForm, LogTimeForm, RecurringTaskForm,
    RescheduleForm, StatusForm, TaskAttachmentForm, TaskEditForm, TaskForm, UnplannedForm, VoidForm, it_staff_queryset,
    parse_local_date,
)
from .models import PRIORITY_RANK, Priority, RecurringTask, Task, TaskAttachment, TaskCategory, TaskTimeEntry

PAGE_SIZE = 25
it_staff_required = role_required(*IT_STAFF_ROLES)

PRIORITY_ORDER = Case(*[When(priority=p, then=Value(r)) for p, r in PRIORITY_RANK.items()],
                      default=Value(9), output_field=IntegerField())
SORTS = {
    "date": ("planned_date", "scheduled_start", "id"),
    "-date": ("-planned_date", "-scheduled_start", "-id"),
    "priority": ("priority_order", "planned_date"),
    "number": ("id",),
    "-number": ("-id",),
    "title": ("title",),
    "status": ("status", "planned_date"),
    "assigned": ("assigned_to__first_name", "planned_date"),
}
VIEWS = {
    "mine": "My open tasks",
    "today": "Today",
    "overdue": "Overdue",
    "unplanned": "Unplanned work",
    "all": "All tasks",
}


def _apply_errors(form, exc):
    """Copy a service ValidationError onto a form so the page shows it next to the right field."""
    if hasattr(exc, "error_dict"):
        for field, errs in exc.error_dict.items():
            target = field if field in form.fields else None
            for e in errs:
                form.add_error(target, e)
    else:
        for msg in exc.messages:
            form.add_error(None, msg)


def _get_task(pk):
    return get_object_or_404(Task.objects.select_related("category", "assigned_to", "created_by", "department",
                                                         "location", "interrupted_task", "recurrence"), pk=pk)


def _safe_next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@it_staff_required
def task_list(request):
    recurrence.ensure_generated()
    today = timezone.localdate()
    view = request.GET.get("view", "mine")
    if view not in VIEWS:
        view = "mine"
    qs = Task.objects.select_related("category", "assigned_to", "department").annotate(priority_order=PRIORITY_ORDER)

    if view == "mine":
        qs = qs.open().filter(assigned_to=request.user)
    elif view == "today":
        qs = qs.filter(planned_date=today)
    elif view == "overdue":
        qs = qs.overdue(today)
    elif view == "unplanned":
        qs = qs.filter(is_unplanned=True)

    f = {k: request.GET.get(k, "").strip() for k in
         ("q", "status", "category", "priority", "assigned", "department", "start", "end", "late", "done")}
    if f["q"]:
        qs = qs.filter(Q(title__icontains=f["q"]) | Q(number__icontains=f["q"]) | Q(description__icontains=f["q"]))
    if f["status"] == "open":
        qs = qs.open()
    elif f["status"] in Task.Status.values:
        qs = qs.filter(status=f["status"])
    if f["category"].isdigit():
        qs = qs.filter(category_id=f["category"])
    if f["priority"] in Priority.values:
        qs = qs.filter(priority=f["priority"])
    if f["assigned"].isdigit():
        qs = qs.filter(assigned_to_id=f["assigned"])
    if f["department"].isdigit():
        qs = qs.filter(department_id=f["department"])
    start, end = parse_local_date(f["start"]), parse_local_date(f["end"])
    if start:
        qs = qs.filter(planned_date__gte=start)
    if end:
        qs = qs.filter(planned_date__lte=end)
    done = parse_local_date(f["done"])
    if done:
        qs = qs.filter(status=Task.Status.COMPLETED).annotate(completed_day=TruncDate("completed_at")).filter(
            completed_day=done)
    if f["late"] == "1":
        qs = qs.filter(status=Task.Status.COMPLETED).annotate(done_day=TruncDate("completed_at")).filter(
            done_day__gt=F("planned_date"))

    sort = request.GET.get("sort", "-date" if view in ("all", "unplanned") else "date")
    qs = qs.order_by(*SORTS.get(sort, SORTS["date"]))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "tasks/task_list.html", {
        "page_obj": page, "view": view, "views": VIEWS, "f": f, "sort": sort, "today": today,
        "statuses": Task.Status.choices, "priorities": Priority.choices,
        "categories": TaskCategory.objects.all(), "officers": it_staff_queryset(),
        "departments": Department.objects.active(),
    })


# ---------------------------------------------------------------------------
# Create / detail / edit
# ---------------------------------------------------------------------------
@it_staff_required
def task_create(request):
    if request.method == "POST":
        form = TaskForm(request.POST, user=request.user)
        if form.is_valid():
            task = form.save(commit=False)
            try:
                services.create_task(task, request.user)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Task {task.number} created.")
                return redirect(_safe_next(request, task.get_absolute_url()))
    else:
        initial = {}
        day = parse_local_date(request.GET.get("date"))
        if day:
            initial["planned_date"] = day
        project = request.GET.get("project", "")
        if project.isdigit():
            initial["project"] = int(project)
        asset = _asset_from_query(request)
        if asset:
            initial.update({"asset": asset.pk, "department": asset.department_id, "location": asset.location_id})
        ticket = _ticket_from_query(request)
        if ticket:
            initial.update({"related_ticket": ticket.pk, "title": f"{ticket.reference}: {ticket.subject}"[:200],
                            "description": ticket.description, "department": ticket.department_id,
                            "location": ticket.location_id, "priority": ticket.priority, "asset": ticket.asset_id,
                            "category": TaskCategory.objects.filter(name="User support").values_list("pk", flat=True).first()})
        form = TaskForm(user=request.user, initial=initial)
    ticket = form["related_ticket"].value()
    from apps.helpdesk.models import Ticket

    ticket = Ticket.objects.filter(pk=ticket).first() if str(ticket or "").isdigit() else None
    return render(request, "tasks/task_form.html", {"form": form, "creating": True, "ticket": ticket})


def _asset_from_query(request):
    from apps.assets.models import Asset

    aid = request.GET.get("asset", "")
    return Asset.objects.filter(pk=aid).first() if aid.isdigit() else None


def _ticket_from_query(request):
    from apps.helpdesk.models import Ticket

    tid = request.GET.get("ticket", "")
    return Ticket.objects.filter(pk=tid).first() if tid.isdigit() else None


@it_staff_required
def task_detail(request, pk):
    task = _get_task(pk)
    entries = task.time_entries.select_related("user", "voided_by").order_by("started_at")
    ticket_entries = task.ticket_time_entries.select_related("user", "voided_by", "ticket").order_by("started_at")
    timeline = []
    for c in task.comments.select_related("author"):
        timeline.append({"at": c.created_at, "kind": c.kind, "label": c.get_kind_display(), "who": c.author,
                         "body": c.body})
    for e in entries:
        timeline.append({"at": e.started_at, "kind": "time", "label": "Time", "who": e.user, "entry": e})
    for e in ticket_entries:
        timeline.append({"at": e.started_at, "kind": "time", "label": f"Time (on {e.ticket.reference})", "who": e.user,
                         "entry": e, "ticket_entry": True})
    timeline.sort(key=lambda x: x["at"])
    running = entries.filter(ended_at__isnull=True, is_voided=False).first() or \
        ticket_entries.filter(ended_at__isnull=True, is_voided=False).first()
    return render(request, "tasks/task_detail.html", {
        "task": task, "timeline": timeline, "entries": entries,
        "recorded": format_minutes(task.recorded_minutes),
        "running": running,
        "attachments": task.attachments.filter(is_deleted=False).select_related("uploaded_by"),
        "attach_form": TaskAttachmentForm(),
        "link_form": LinkTicketForm(),
        "can_modify": services.can_modify(request.user, task),
        "schedule_changes": task.schedule_changes.select_related("changed_by"),
        "interruptions": task.interruptions.all(),
        "comment_form": CommentForm(),
        "today": timezone.localdate(),
    })


@it_staff_required
def task_edit(request, pk):
    task = _get_task(pk)
    services.require_modify(request.user, task)
    before = Task.objects.get(pk=pk)
    if request.method == "POST":
        form = TaskEditForm(request.POST, instance=task)
        if form.is_valid():
            try:
                services.update_task(form.save(commit=False), before, request.user)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Task {task.number} updated.")
                return redirect(task)
    else:
        form = TaskEditForm(instance=task)
    return render(request, "tasks/task_form.html", {"form": form, "task": task, "creating": False})


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _action(request, pk, form_class, template, handler, success, initial=None):
    task = _get_task(pk)
    services.require_modify(request.user, task)
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            try:
                handler(task, form.cleaned_data)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, success.format(task=task))
                return redirect(_safe_next(request, task.get_absolute_url()))
    else:
        form = form_class(initial=initial(task) if initial else None)
    return render(request, template, {"form": form, "task": task, "next": request.GET.get("next", "")})


@it_staff_required
def task_status(request, pk):
    def handler(task, d):
        services.change_status(task, d["status"], request.user, reason=d["reason"],
                               follow_up_action=d["follow_up_action"] or None)
    return _action(request, pk, StatusForm, "tasks/action_status.html", handler, "Status of {task.number} updated.",
                   initial=lambda t: {"status": request.GET.get("to") or t.status, "follow_up_action": t.follow_up_action})


@it_staff_required
def task_complete(request, pk):
    def handler(task, d):
        services.change_status(task, Task.Status.COMPLETED, request.user, completion_notes=d["completion_notes"],
                               follow_up_action=d["follow_up_action"])
    return _action(request, pk, CompleteForm, "tasks/action_complete.html", handler, "Task {task.number} completed.",
                   initial=lambda t: {"follow_up_action": t.follow_up_action})


@it_staff_required
def task_reschedule(request, pk):
    def handler(task, d):
        services.reschedule(task, request.user, new_date=d["new_date"], new_start=d["new_start"],
                            new_end=d["new_end"], reason=d["reason"])
    return _action(request, pk, RescheduleForm, "tasks/action_reschedule.html", handler,
                   "Task {task.number} rescheduled.",
                   initial=lambda t: {"new_date": max(t.planned_date, timezone.localdate()) + timedelta(days=1),
                                      "new_start": t.scheduled_start, "new_end": t.scheduled_end})


@it_staff_required
def task_log_time(request, pk):
    def handler(task, d):
        services.log_time(task, request.user, started_at=d["started_at"], minutes=d["minutes"], note=d["note"])
    return _action(request, pk, LogTimeForm, "tasks/action_log_time.html", handler, "Time recorded on {task.number}.",
                   initial=lambda t: {"started_at": timezone.localtime().replace(second=0, microsecond=0)
                                      - timedelta(minutes=30), "minutes": 30})


@it_staff_required
@require_POST
def task_comment(request, pk):
    task = _get_task(pk)
    form = CommentForm(request.POST)
    if form.is_valid():
        services.add_comment(task, request.user, form.cleaned_data["body"])
        messages.success(request, "Comment added.")
    else:
        messages.error(request, "The comment was empty.")
    return redirect(task)


@it_staff_required
@require_POST
def timer_start(request, pk):
    task = _get_task(pk)
    try:
        entry, stopped = services.start_timer(task, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if stopped:
            messages.info(request, f"Stopped the timer on {stopped.task.number} ({format_minutes(stopped.minutes)}).")
        messages.success(request, f"Timer started on {task.number}.")
    return redirect(_safe_next(request, task.get_absolute_url()))


@it_staff_required
@require_POST
def timer_stop(request):
    try:
        entry = services.stop_timer(request.user, note=request.POST.get("note", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(_safe_next(request, reverse("tasks:planner")))
    messages.success(request, f"Recorded {format_minutes(entry.minutes)} on {entry.task.number}.")
    warning = services.timer_warning(entry)
    if warning:
        messages.warning(request, warning)
    return redirect(_safe_next(request, entry.task.get_absolute_url()))


@it_staff_required
def time_void(request, entry_pk):
    entry = get_object_or_404(TaskTimeEntry.objects.select_related("task", "task__category"), pk=entry_pk)
    # Ticket time recorded through a task is voided on the ticket (helpdesk:time_void).
    if request.method == "POST":
        form = VoidForm(request.POST)
        if form.is_valid():
            try:
                services.void_time_entry(entry, request.user, form.cleaned_data["reason"])
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, "Time entry voided. It no longer counts in totals but stays in the history.")
                return redirect(entry.task)
    else:
        services.require_modify(request.user, entry.task)
        form = VoidForm()
    return render(request, "tasks/action_void.html", {"form": form, "entry": entry, "task": entry.task})


@it_staff_required
def unplanned_create(request):
    if request.method == "POST":
        form = UnplannedForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            task = form.save(commit=False)
            try:
                with transaction.atomic():
                    services.record_unplanned(
                        task, request.user, interrupted_task=d["interrupted_task"],
                        start_timer_now=d["mode"] == UnplannedForm.MODE_TIMER and task.assigned_to_id == request.user.pk,
                        worked_from=d["worked_from"] if d["mode"] == UnplannedForm.MODE_DONE else None,
                        worked_minutes=d["worked_minutes"] if d["mode"] == UnplannedForm.MODE_DONE else None,
                        completion_notes=d["completion_notes"] if d["mode"] == UnplannedForm.MODE_DONE else "",
                    )
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Unplanned work recorded as {task.number}.")
                return redirect(task)
    else:
        initial = {}
        interrupted = request.GET.get("interrupts")
        if interrupted and interrupted.isdigit():
            initial["interrupted_task"] = int(interrupted)
        form = UnplannedForm(user=request.user, initial=initial)
    return render(request, "tasks/unplanned_form.html", {"form": form})


# ---------------------------------------------------------------------------
# Evidence and ticket links
# ---------------------------------------------------------------------------
@it_staff_required
@require_POST
def task_attach(request, pk):
    task = _get_task(pk)
    form = TaskAttachmentForm(request.POST, request.FILES)
    if form.is_valid() and form.uploads:
        services.add_attachments(task, form.uploads, request.user, form.cleaned_data["description"])
        messages.success(request, f"{len(form.uploads)} file(s) attached.")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        if form.is_valid():
            messages.error(request, "Choose at least one file.")
    return redirect(task)


@it_staff_required
def task_attachment(request, pk):
    att = get_object_or_404(TaskAttachment.objects.select_related("task"), pk=pk)
    if att.is_deleted and not request.user.is_superuser:
        raise PermissionDenied("This file has been removed.")
    from apps.evidence import serving

    return serving.serve(request, att, download=request.GET.get("download") == "1")


@it_staff_required
def task_attachment_hide(request, pk):
    att = get_object_or_404(TaskAttachment.objects.select_related("task"), pk=pk)
    services.require_modify(request.user, att.task)
    if request.method == "POST":
        form = VoidForm(request.POST)
        if form.is_valid():
            services.hide_attachment(att, request.user, form.cleaned_data["reason"])
            messages.success(request, "File removed from view. It is kept for the audit record.")
            return redirect(att.task)
    else:
        form = VoidForm()
    return render(request, "tasks/action_void.html", {"form": form, "task": att.task, "attachment": att})


@it_staff_required
@require_POST
def task_link_ticket(request, pk):
    task = _get_task(pk)
    form = LinkTicketForm(request.POST)
    if form.is_valid():
        try:
            services.link_to_ticket(task, form.ticket, request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"Linked to {form.ticket.reference}. Time on this task is now recorded on the ticket.")
    else:
        messages.error(request, " ".join(form.errors.get("reference", ["Enter a ticket reference."])))
    return redirect(task)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
def _planner_user(request):
    """Whose plan to show. Everyone sees their own; colleagues' plans are viewable by IT staff."""
    uid = request.GET.get("user")
    if uid and uid.isdigit():
        user = it_staff_queryset().filter(pk=uid).first()
        if user:
            return user
    return request.user


@it_staff_required
def planner_day(request):
    from apps.core.jobs import ensure_jobs_ran

    recurrence.ensure_generated()
    ensure_jobs_ran()  # creates due maintenance tasks, reminders, etc.
    today = timezone.localdate()
    day = parse_local_date(request.GET.get("date")) or today
    owner = _planner_user(request)
    base = Task.objects.filter(assigned_to=owner).select_related("category", "interrupted_task")
    tasks = list(base.filter(planned_date=day).annotate(priority_order=PRIORITY_ORDER)
                 .order_by("scheduled_start", "priority_order", "id"))
    scheduled = [t for t in tasks if t.scheduled_start]
    unscheduled = [t for t in tasks if not t.scheduled_start]
    overdue = list(base.overdue(today).exclude(planned_date=day).order_by("planned_date")) if day == today else []
    moved_away = (Task.objects.filter(schedule_changes__old_date=day, assigned_to=owner)
                  .exclude(planned_date=day).distinct().prefetch_related("schedule_changes"))
    unfinished = [t for t in tasks if not t.is_closed]
    start = timezone.make_aware(datetime.combine(day, time.min))
    end = timezone.make_aware(datetime.combine(day, time.max))
    minutes = TaskTimeEntry.objects.filter(user=owner, is_voided=False, ended_at__isnull=False,
                                           started_at__range=(start, end)).aggregate(m=Sum("minutes"))["m"] or 0
    planned = [t for t in tasks if not t.is_unplanned]
    return render(request, "tasks/planner_day.html", {
        "day": day, "today": today, "owner": owner, "officers": it_staff_queryset(),
        "scheduled": scheduled, "unscheduled": unscheduled, "overdue": overdue, "moved_away": moved_away,
        "unfinished": unfinished,
        "stats": {
            "planned": len(planned), "done": sum(1 for t in planned if t.status == Task.Status.COMPLETED),
            "unplanned": sum(1 for t in tasks if t.is_unplanned), "time": format_minutes(minutes),
            "estimated": format_minutes(sum(t.estimated_minutes or 0 for t in tasks)),
        },
        "prev": day - timedelta(days=1), "next": day + timedelta(days=1),
        "carry_form": BulkRescheduleForm(initial={"new_date": max(day, today) + timedelta(days=1)}),
        "is_own": owner == request.user,
    })


@it_staff_required
@require_POST
def planner_carry_over(request):
    form = BulkRescheduleForm(request.POST)
    day = parse_local_date(request.POST.get("day"))
    ids = [int(i) for i in request.POST.getlist("task") if i.isdigit()]
    if not form.is_valid() or not day or not ids:
        messages.error(request, "Select at least one task, a date and a reason.")
        return redirect(reverse("tasks:planner") + (f"?date={day:%Y-%m-%d}" if day else ""))
    tasks = Task.objects.filter(pk__in=ids, planned_date=day).select_related("category")
    moved, skipped = services.bulk_reschedule(tasks, request.user, new_date=form.cleaned_data["new_date"],
                                              reason=form.cleaned_data["reason"])
    if moved:
        messages.success(request, f"{len(moved)} task(s) moved to {form.cleaned_data['new_date']:%d %b %Y}.")
    if skipped:
        messages.warning(request, f"{len(skipped)} task(s) could not be moved (closed or not yours): "
                         + ", ".join(t.number for t in skipped))
    return redirect(reverse("tasks:planner") + f"?date={day:%Y-%m-%d}")


@it_staff_required
def planner_week(request):
    recurrence.ensure_generated()
    today = timezone.localdate()
    anchor = parse_local_date(request.GET.get("date")) or today
    monday = anchor - timedelta(days=anchor.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    owner = _planner_user(request)
    show_all = request.GET.get("user") == "all"
    qs = Task.objects.filter(planned_date__range=(days[0], days[-1])).select_related("category", "assigned_to")
    if not show_all:
        qs = qs.filter(assigned_to=owner)
    qs = qs.annotate(priority_order=PRIORITY_ORDER).order_by("scheduled_start", "priority_order")
    by_day = {d: [] for d in days}
    for t in qs:
        by_day[t.planned_date].append(t)
    return render(request, "tasks/planner_week.html", {
        "days": [(d, by_day[d]) for d in days], "today": today, "owner": owner, "show_all": show_all,
        "officers": it_staff_queryset(), "prev": monday - timedelta(days=7), "next": monday + timedelta(days=7),
        "monday": monday,
    })


# ---------------------------------------------------------------------------
# Recurring schedules
# ---------------------------------------------------------------------------
@it_staff_required
def recurring_list(request):
    status = request.GET.get("status", "active")
    qs = RecurringTask.objects.select_related("category", "assigned_to")
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    return render(request, "tasks/recurring_list.html", {"items": qs, "status": status})


@it_staff_required
def recurring_edit(request, pk=None):
    obj = get_object_or_404(RecurringTask, pk=pk) if pk else None
    if obj and not (request.user.profile.role == Role.ADMIN or request.user.is_superuser
                    or request.user.pk in (obj.created_by_id, obj.assigned_to_id)):
        audit.log(AuditLog.Action.ACCESS_DENIED, obj, message="Attempted to edit a recurring schedule")
        raise PermissionDenied("You can only change schedules you created or that are assigned to you.")
    before = RecurringTask.objects.get(pk=pk) if pk else None
    if request.method == "POST":
        form = RecurringTaskForm(request.POST, instance=obj)
        if form.is_valid():
            with transaction.atomic():
                item = form.save(commit=False)
                if not obj:
                    item.created_by = request.user
                item.save()
                if obj:
                    changes = audit.diff_instances(before, item, [f for f in form.Meta.fields])
                    audit.log(AuditLog.Action.UPDATE, item, changes=changes, message="Recurring schedule edited")
                    removed, created = recurrence.refresh_template(item, request.user)
                else:
                    audit.log(AuditLog.Action.CREATE, item, message=f"Recurring schedule created: {item.describe()}")
                    removed, created = 0, recurrence.generate_for(item)
            messages.success(request, f"Schedule saved. {created} task(s) generated"
                             + (f", {removed} untouched future task(s) replaced." if removed else "."))
            return redirect("tasks:recurring_list")
    else:
        form = RecurringTaskForm(instance=obj, initial={"assigned_to": request.user.pk} if not obj else None)
    upcoming = obj.occurrences.filter(planned_date__gte=timezone.localdate()).order_by("planned_date")[:10] if obj else []
    return render(request, "tasks/recurring_form.html", {"form": form, "obj": obj, "upcoming": upcoming})


# ---------------------------------------------------------------------------
# Categories (administrators)
# ---------------------------------------------------------------------------
@role_required(Role.ADMIN)
def category_list(request):
    return render(request, "tasks/category_list.html", {"items": TaskCategory.objects.all()})


@role_required(Role.ADMIN)
def category_edit(request, pk=None):
    obj = get_object_or_404(TaskCategory, pk=pk) if pk else None
    before = TaskCategory.objects.get(pk=pk) if pk else None
    if request.method == "POST":
        form = CategoryForm(request.POST, instance=obj)
        if form.is_valid():
            item = form.save()
            if obj:
                changes = audit.diff_instances(before, item, ["name", "activity_type", "description", "is_active"])
                if changes:
                    audit.log(AuditLog.Action.CONFIG_CHANGE, item, changes=changes, message="Task category changed")
            else:
                audit.log(AuditLog.Action.CONFIG_CHANGE, item, message="Task category created")
            messages.success(request, f"Category '{item}' saved.")
            return redirect("tasks:category_list")
    else:
        form = CategoryForm(instance=obj)
    return render(request, "accounts/simple_form.html", {"form": form, "obj": obj, "label": "Task category",
                                                         "label_plural": "Task categories",
                                                         "list_url": "tasks:category_list"})
