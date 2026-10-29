from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import Department
from apps.accounts.roles import IT_STAFF_ROLES, Role, get_role, is_it_staff
from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required
from apps.diary.timecalc import format_minutes
from apps.evidence import serving
from apps.tasks.forms import it_staff_queryset, parse_local_date
from apps.tasks.models import PRIORITY_RANK, Priority
from apps.tasks.views import _apply_errors, _safe_next

from . import services
from .forms import (
    AssignForm, CommentForm, ConfirmForm, EscalationRuleForm, HideAttachmentForm, LogTimeForm, PriorityForm,
    ReopenForm, SLATargetForm, StatusForm, TicketCategoryForm, TicketCreateForm, VoidForm,
)
from .models import (
    EscalationRule, SLATarget, Ticket, TicketAttachment, TicketCategory, TicketComment, TicketTimeEntry,
)

PAGE_SIZE = 25
it_required = role_required(*IT_STAFF_ROLES)
S = Ticket.Status

PRIORITY_ORDER = Case(*[When(priority=p, then=Value(r)) for p, r in PRIORITY_RANK.items()],
                      default=Value(9), output_field=IntegerField())
QUEUES = {
    "open": "Open", "new": "New", "mine": "Assigned to me", "unassigned": "Unassigned", "critical": "Critical / high",
    "overdue": "Overdue", "waiting": "Waiting", "resolved": "Awaiting confirmation", "closed": "Closed", "all": "All",
}
SORTS = {"-created": ("-created_at",), "created": ("created_at",), "priority": ("priority_order", "created_at"),
         "due": ("resolution_due_at",), "status": ("status", "-created_at"), "reference": ("reference",)}


def _get(pk, user):
    ticket = get_object_or_404(Ticket.objects.select_related(
        "requester", "requester__profile", "assigned_to", "department", "location", "category", "subcategory",
        "asset"), pk=pk)
    services.require_view(user, ticket)
    return ticket


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------
@login_required
def ticket_list(request):
    user = request.user
    it = is_it_staff(user)
    if it:
        services.ensure_jobs_ran()
    role = get_role(user)
    qs = Ticket.objects.visible_to(user).select_related("requester", "assigned_to", "department", "category") \
        .annotate(priority_order=PRIORITY_ORDER)

    default_queue = "open" if it else "mine"
    queue = request.GET.get("queue", default_queue)
    queues = QUEUES if it else {"mine": "My requests", **({"department": "My department"} if role == Role.MANAGER else {}),
                                "all": "All I can see"}
    if queue not in queues:
        queue = default_queue
    now = timezone.now()
    if queue == "open":
        qs = qs.open()
    elif queue == "new":
        qs = qs.filter(status=S.NEW)
    elif queue == "mine":
        qs = qs.filter(assigned_to=user).open() if it else qs.filter(requester=user)
    elif queue == "unassigned":
        qs = qs.open().filter(assigned_to__isnull=True)
    elif queue == "critical":
        qs = qs.open().filter(priority__in=[Priority.CRITICAL, Priority.HIGH])
    elif queue == "overdue":
        qs = qs.overdue(now)
    elif queue == "waiting":
        qs = qs.filter(status=S.WAITING)
    elif queue == "resolved":
        qs = qs.filter(status=S.RESOLVED)
    elif queue == "closed":
        qs = qs.filter(status=S.CLOSED)
    elif queue == "department":
        qs = qs.filter(department_id=user.profile.department_id)

    f = {k: request.GET.get(k, "").strip() for k in
         ("q", "type", "category", "priority", "status", "department", "assigned", "start", "end")}
    if f["q"]:
        qs = qs.filter(Q(reference__icontains=f["q"]) | Q(subject__icontains=f["q"]) | Q(description__icontains=f["q"])
                       | Q(affected_device__icontains=f["q"]) | Q(requester__first_name__icontains=f["q"])
                       | Q(requester__last_name__icontains=f["q"]))
    if f["type"] in Ticket.Type.values:
        qs = qs.filter(ticket_type=f["type"])
    if f["category"].isdigit():
        qs = qs.filter(Q(category_id=f["category"]) | Q(subcategory_id=f["category"]))
    if f["priority"] in Priority.values:
        qs = qs.filter(priority=f["priority"])
    if f["status"] in S.values:
        qs = qs.filter(status=f["status"])
    if it and f["department"].isdigit():
        qs = qs.filter(department_id=f["department"])
    if it and f["assigned"].isdigit():
        qs = qs.filter(assigned_to_id=f["assigned"])
    tz = timezone.get_current_timezone()
    start, end = parse_local_date(f["start"]), parse_local_date(f["end"])
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz))
    if end:
        qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(end, time.max), tz))
    resolved_on = parse_local_date(request.GET.get("resolved"))
    if resolved_on:
        qs = qs.filter(resolved_at__range=(timezone.make_aware(datetime.combine(resolved_on, time.min), tz),
                                           timezone.make_aware(datetime.combine(resolved_on, time.max), tz)))

    sort = request.GET.get("sort", "priority" if it and queue in ("open", "mine", "critical") else "-created")
    qs = qs.order_by(*SORTS.get(sort, SORTS["-created"]))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "helpdesk/ticket_list.html", {
        "page_obj": page, "queue": queue, "queues": queues, "f": f, "sort": sort, "it": it,
        "types": Ticket.Type.choices, "priorities": Priority.choices, "statuses": S.choices,
        "categories": TicketCategory.objects.filter(parent__isnull=True),
        "departments": Department.objects.active() if it else [], "officers": it_staff_queryset() if it else [],
        "resolved_on": resolved_on,
    })


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def _asset_from_token(value):
    from apps.assets.models import Asset

    try:
        return Asset.objects.in_service().filter(qr_token=value).first() if value else None
    except ValidationError:  # not a valid UUID
        return None


@login_required
def ticket_create(request):
    it = is_it_staff(request.user)
    asset = _asset_from_token(request.POST.get("asset_token") or request.GET.get("asset"))
    if request.method == "POST":
        form = TicketCreateForm(request.POST, request.FILES, user=request.user, it_mode=it)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.asset = asset
            on_behalf = form.cleaned_data.get("requester") if it else None
            if on_behalf == request.user:
                on_behalf = None
            try:
                services.create_ticket(ticket, request.user, form.uploads, on_behalf_of=on_behalf)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Thank you. Your request has been logged as {ticket.reference}. "
                                          "IT will be in touch; you can follow progress here.")
                return redirect(ticket)
    else:
        initial = {"requester": request.user.pk} if it else {}
        if asset:
            initial.update({"affected_device": f"{asset.tag} {asset.name}"[:150], "location": asset.location_id})
        form = TicketCreateForm(user=request.user, it_mode=it, initial=initial or None)
    return render(request, "helpdesk/ticket_form.html", {"form": form, "subcategories": form.subcategory_map(),
                                                         "asset": asset})


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@login_required
def ticket_detail(request, pk):
    ticket = _get(pk, request.user)
    it = is_it_staff(request.user)
    comments = ticket.comments.select_related("author")
    attachments = ticket.attachments.select_related("uploaded_by").filter(is_deleted=False)
    if not it:
        comments = comments.exclude(kind__in=TicketComment.INTERNAL_KINDS)
        attachments = attachments.filter(is_internal=False)
    by_comment = {}
    initial_files = []
    for a in attachments:
        if a.comment_id:
            by_comment.setdefault(a.comment_id, []).append(a)
        else:
            initial_files.append(a)

    timeline = [{"at": c.created_at, "type": "comment", "obj": c, "files": by_comment.get(c.pk, [])} for c in comments]
    timeline += [{"at": h.changed_at, "type": "status", "obj": h} for h in ticket.status_history.select_related("changed_by")
                 if h.from_status]  # creation is shown separately
    entries = []
    if it:
        entries = list(ticket.time_entries.select_related("user", "via_task", "voided_by").order_by("started_at"))
        timeline += [{"at": e.started_at, "type": "time", "obj": e} for e in entries]
    timeline.sort(key=lambda x: x["at"])
    is_req = services.is_requester(request.user, ticket)
    running = next((e for e in entries if e.is_running and e.user_id == request.user.pk), None)
    return render(request, "helpdesk/ticket_detail.html", {
        "ticket": ticket, "it": it, "is_requester": is_req, "timeline": timeline,
        "initial_files": initial_files,  # files sent with the original report
        "comment_form": CommentForm(it=it), "assign_form": AssignForm(initial={"assigned_to": ticket.assigned_to_id}),
        "recorded": format_minutes(ticket.recorded_minutes) if it else None, "running": running,
        "tasks": ticket.tasks.select_related("assigned_to") if it else [],
        "assets": _asset_choices(ticket) if it else [],
        "projects": _project_choices(ticket) if it else [],
        "suggestions": _suggestions(ticket) if it else [],
        "can_reopen": services.can_reopen(request.user, ticket),
        "can_comment": ticket.status != S.CLOSED or it,
        "transitions": sorted(services.TRANSITIONS.get(ticket.status, []), key=list(S.values).index) if it else [],
        "S": S,
    })


def _project_choices(ticket):
    from apps.projects.models import Project

    return Project.objects.filter(Q(status__in=["proposed", "approved", "in_progress", "on_hold"]) | Q(pk=ticket.project_id))


def _suggestions(ticket):
    from apps.knowledge.matching import suggest

    applied = set(ticket.applied_articles.values_list("pk", flat=True))
    return [(score, article, reasons, article.pk in applied) for score, article, reasons in suggest(ticket)]


def _asset_choices(ticket):
    from apps.assets.models import Asset

    qs = Asset.objects.in_service().only("pk", "tag", "name")
    if ticket.department_id:  # likely candidates first
        return sorted(qs, key=lambda a: (a.department_id != ticket.department_id, a.tag))
    return list(qs)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
@login_required
@require_POST
def ticket_comment(request, pk):
    ticket = _get(pk, request.user)
    it = is_it_staff(request.user)
    form = CommentForm(request.POST, request.FILES, it=it)
    if form.is_valid():
        try:
            services.add_comment(ticket, request.user, form.cleaned_data["body"],
                                 kind=form.cleaned_data.get("kind") or TicketComment.Kind.PUBLIC, uploads=form.uploads,
                                 stage=form.cleaned_data.get("stage") or None,
                                 internal_files=form.cleaned_data.get("internal_files", False))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Added to the ticket.")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect(reverse("helpdesk:detail", args=[ticket.pk]) + "#conversation")


def _ticket_action(request, pk, form_class, template, handler, success, *, it_only=True, initial=None, extra=None):
    ticket = _get(pk, request.user)
    if it_only:
        services.require_it(request.user, ticket)
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            try:
                handler(ticket, form.cleaned_data)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, success.format(ticket=ticket))
                return redirect(_safe_next(request, ticket.get_absolute_url()))
    else:
        form = form_class(initial=initial(ticket) if initial else None)
    ctx = {"form": form, "ticket": ticket}
    ctx.update(extra or {})
    return render(request, template, ctx)


@login_required
def ticket_status(request, pk):
    def handler(ticket, d):
        services.change_status(ticket, d["status"], request.user, note=d["note"], waiting_on=d["waiting_on"],
                               resolution_details=d["resolution_details"], root_cause=d["root_cause"],
                               verification=d["verification"])
    return _ticket_action(request, pk, StatusForm, "helpdesk/action_status.html", handler,
                          "{ticket.reference} updated.", initial=lambda t: {"status": request.GET.get("to") or t.status})


@login_required
def ticket_priority(request, pk):
    def handler(ticket, d):
        services.change_priority(ticket, d["priority"], request.user, d["reason"])
    return _ticket_action(request, pk, PriorityForm, "helpdesk/action_simple.html", handler,
                          "Priority of {ticket.reference} changed.", initial=lambda t: {"priority": t.priority},
                          extra={"heading": "Change priority", "button": "Change priority",
                                 "intro": "Response and resolution targets are recalculated from when the ticket was reported."})


@login_required
def ticket_confirm(request, pk):
    def handler(ticket, d):
        services.confirm_resolution(ticket, request.user, rating=d["rating"], comment=d["comment"])
    return _ticket_action(request, pk, ConfirmForm, "helpdesk/action_simple.html", handler,
                          "Thank you for confirming. {ticket.reference} is now closed.", it_only=False,
                          extra={"heading": "Confirm the problem is fixed", "button": "Yes, it's fixed",
                                 "button_class": "btn-success"})


@login_required
def ticket_reopen(request, pk):
    def handler(ticket, d):
        services.reopen(ticket, request.user, d["reason"])
    ticket = _get(pk, request.user)
    if not services.can_reopen(request.user, ticket):
        raise PermissionDenied("This ticket can no longer be reopened. Please report a new problem.")
    return _ticket_action(request, pk, ReopenForm, "helpdesk/action_simple.html", handler,
                          "{ticket.reference} has been reopened. IT has been notified.", it_only=False,
                          extra={"heading": "Reopen ticket", "button": "Reopen", "button_class": "btn-warning"})


@login_required
@require_POST
def ticket_assign(request, pk):
    ticket = _get(pk, request.user)
    services.require_it(request.user, ticket)
    if request.POST.get("me"):
        assignee = request.user
    else:
        form = AssignForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Choose a valid IT staff member.")
            return redirect(ticket)
        assignee = form.cleaned_data["assigned_to"]
    try:
        services.assign(ticket, assignee, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"{ticket.reference} assigned to {assignee.get_full_name() or assignee.username}."
                         if assignee else f"{ticket.reference} is now unassigned.")
    return redirect(ticket)


@login_required
@require_POST
def ticket_set_asset(request, pk):
    from apps.assets.models import Asset

    ticket = _get(pk, request.user)
    services.require_it(request.user, ticket)
    value = request.POST.get("asset", "")
    asset = Asset.objects.filter(pk=value).first() if value.isdigit() else None
    services.set_asset(ticket, asset, request.user)
    messages.success(request, f"Linked to {asset}." if asset else "Asset link removed.")
    return redirect(ticket)


@login_required
@require_POST
def ticket_set_project(request, pk):
    from apps.projects.models import Project

    ticket = _get(pk, request.user)
    services.require_it(request.user, ticket)
    value = request.POST.get("project", "")
    project = Project.objects.filter(pk=value).first() if value.isdigit() else None
    services.set_project(ticket, project, request.user)
    messages.success(request, f"Linked to project {project.code}." if project else "Project link removed.")
    return redirect(ticket)


@login_required
@require_POST
def ticket_timer_start(request, pk):
    ticket = _get(pk, request.user)
    try:
        _entry, stopped = services.start_timer(ticket, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if stopped:
            messages.info(request, f"Stopped the previous timer ({format_minutes(stopped.minutes)}).")
        messages.success(request, f"Timer started on {ticket.reference}.")
    return redirect(_safe_next(request, ticket.get_absolute_url()))


@login_required
def ticket_log_time(request, pk):
    def handler(ticket, d):
        services.log_time(ticket, request.user, started_at=d["started_at"], minutes=d["minutes"], note=d["note"])
    return _ticket_action(request, pk, LogTimeForm, "helpdesk/action_simple.html", handler,
                          "Time recorded on {ticket.reference}.",
                          initial=lambda t: {"started_at": timezone.localtime().replace(second=0, microsecond=0)
                                             - timedelta(minutes=30), "minutes": 30},
                          extra={"heading": "Record time worked", "button": "Record time",
                                 "intro": "Time that overlaps time you already recorded is refused."})


@login_required
def time_void(request, entry_pk):
    entry = get_object_or_404(TicketTimeEntry.objects.select_related("ticket", "via_task"), pk=entry_pk)
    services.require_view(request.user, entry.ticket)
    services.require_it(request.user, entry.ticket)
    if request.method == "POST":
        form = VoidForm(request.POST)
        if form.is_valid():
            try:
                services.void_time(entry, request.user, form.cleaned_data["reason"])
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, "Time entry voided. It stays in the history but is no longer counted.")
                return redirect(_safe_next(request, entry.ticket.get_absolute_url()))
    else:
        form = VoidForm()
    return render(request, "helpdesk/action_simple.html", {
        "form": form, "ticket": entry.ticket, "heading": "Void time entry", "button": "Void entry",
        "button_class": "btn-danger",
        "intro": f"{timezone.localtime(entry.started_at):%d %b %Y %H:%M}, {entry.minutes} min by "
                 f"{entry.user.get_full_name() or entry.user.username}."})


@login_required
def ticket_create_task(request, pk):
    ticket = _get(pk, request.user)
    services.require_it(request.user, ticket)
    return redirect(reverse("tasks:create") + f"?ticket={ticket.pk}")


# ---------------------------------------------------------------------------
# Evidence files
# ---------------------------------------------------------------------------
@login_required
def attachment_view(request, pk):
    att = get_object_or_404(TicketAttachment.objects.select_related("ticket"), pk=pk)
    services.require_view(request.user, att.ticket)
    if (att.is_internal or att.is_deleted) and not is_it_staff(request.user):
        audit.log(AuditLog.Action.ACCESS_DENIED, att.ticket, message=f"Attempted to open restricted file {att.pk}")
        raise PermissionDenied("This file is not available to you.")
    return serving.serve(request, att, download=request.GET.get("download") == "1")


@login_required
def attachment_hide(request, pk):
    att = get_object_or_404(TicketAttachment.objects.select_related("ticket"), pk=pk)
    services.require_view(request.user, att.ticket)
    services.require_it(request.user, att.ticket)
    if request.method == "POST":
        form = HideAttachmentForm(request.POST)
        if form.is_valid():
            services.hide_attachment(att, request.user, form.cleaned_data["reason"])
            messages.success(request, "File removed from view. It is kept for the audit record.")
            return redirect(att.ticket)
    else:
        form = HideAttachmentForm()
    return render(request, "helpdesk/action_simple.html", {
        "form": form, "ticket": att.ticket, "heading": f"Remove “{att.original_name}”", "button": "Remove from view",
        "button_class": "btn-danger", "intro": "The file is hidden from the ticket but kept for the audit record."})


# ---------------------------------------------------------------------------
# Configuration (administrators)
# ---------------------------------------------------------------------------
@role_required(Role.ADMIN)
def category_list(request):
    tops = TicketCategory.objects.filter(parent__isnull=True).prefetch_related("children")
    return render(request, "helpdesk/category_list.html", {"tops": tops})


@role_required(Role.ADMIN)
def category_edit(request, pk=None):
    obj = get_object_or_404(TicketCategory, pk=pk) if pk else None
    if request.method == "POST":
        form = TicketCategoryForm(request.POST, instance=obj)
        if form.is_valid():
            item = form.save()
            audit.log(AuditLog.Action.CONFIG_CHANGE, item, message=f"Ticket category {'updated' if obj else 'created'}")
            messages.success(request, f"Category “{item}” saved.")
            return redirect("helpdesk:category_list")
    else:
        form = TicketCategoryForm(instance=obj, initial={"parent": request.GET.get("parent")} if not obj else None)
    return render(request, "accounts/simple_form.html", {"form": form, "obj": obj, "label": "Ticket category",
                                                         "label_plural": "Ticket categories",
                                                         "list_url": "helpdesk:category_list"})


@role_required(Role.ADMIN)
def sla_list(request):
    targets = {t.priority: t for t in SLATarget.objects.all()}
    rows = []
    for value, label in Priority.choices:
        target = targets.get(value) or SLATarget(priority=value, response_minutes=services.DEFAULT_TARGETS[value][0],
                                                 resolution_minutes=services.DEFAULT_TARGETS[value][1])
        prefix = f"p-{value}"
        form = SLATargetForm(request.POST or None, instance=target, prefix=prefix)
        rows.append((label, target, form))
    if request.method == "POST":
        if all(form.is_valid() for _l, _t, form in rows):
            for label, target, form in rows:
                before = (target.response_minutes, target.resolution_minutes) if target.pk else None
                saved = form.save()
                after = (saved.response_minutes, saved.resolution_minutes)
                if before != after:
                    audit.log(AuditLog.Action.CONFIG_CHANGE, saved, changes={"targets": [str(before), str(after)]},
                              message=f"Service targets for {label} changed")
            messages.success(request, "Service targets saved. They apply to tickets reported from now on "
                                      "and to tickets whose priority is changed.")
            return redirect("helpdesk:sla_list")
    return render(request, "helpdesk/sla_list.html", {"rows": rows, "rules": EscalationRule.objects.all()})


@role_required(Role.ADMIN)
def rule_edit(request, pk=None):
    obj = get_object_or_404(EscalationRule, pk=pk) if pk else None
    if request.method == "POST":
        form = EscalationRuleForm(request.POST, instance=obj)
        if form.is_valid():
            item = form.save()
            audit.log(AuditLog.Action.CONFIG_CHANGE, item, message=f"Escalation rule {'updated' if obj else 'created'}")
            messages.success(request, f"Escalation rule “{item}” saved.")
            return redirect("helpdesk:sla_list")
    else:
        form = EscalationRuleForm(instance=obj)
    return render(request, "accounts/simple_form.html", {"form": form, "obj": obj, "label": "Escalation rule",
                                                         "label_plural": "Service targets & escalation",
                                                         "list_url": "helpdesk:sla_list"})
