from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.roles import IT_STAFF_ROLES, Role, is_it_staff
from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required
from apps.evidence import serving
from apps.tasks.views import _apply_errors

from . import services
from .forms import AttachmentForm, DecisionForm, DeliveredForm, OrderedForm, ReasonForm, RequestForm, TrackingForm
from .models import ProcurementAttachment, ProcurementRequest

it_required = role_required(*IT_STAFF_ROLES)
viewer_required = role_required(Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)
S = ProcurementRequest.Status


def _get(request, pk):
    req = get_object_or_404(ProcurementRequest.objects.select_related(
        "department", "department__head", "asset", "ticket", "project", "supplier", "requested_by"), pk=pk)
    if not ProcurementRequest.objects.visible_to(request.user).filter(pk=req.pk).exists() \
            and not services.can_approve(request.user, req):
        audit.log(AuditLog.Action.ACCESS_DENIED, req, message="Attempted to open a procurement request without permission")
        raise PermissionDenied("You cannot view this request.")
    return req


@viewer_required
def request_list(request):
    view = request.GET.get("view", "open")
    qs = ProcurementRequest.objects.filter(services.visible_q(request.user) | Q(
        pk__in=services.pending_for(request.user).values("pk"))).select_related("department", "requested_by", "asset")
    if view == "open":
        qs = qs.filter(status__in=ProcurementRequest.OPEN_STATUSES)
    elif view == "approval":
        qs = services.pending_for(request.user).select_related("department", "requested_by", "asset")
    elif view in S.values:
        qs = qs.filter(status=view)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(item__icontains=q) | Q(reason__icontains=q))
    page = Paginator(qs.order_by("-created_at"), 25).get_page(request.GET.get("page"))
    year_start = timezone.localdate().replace(month=1, day=1)
    spend = services.spend_by_currency(ProcurementRequest.objects.filter(
        services.visible_q(request.user), delivery_date__gte=year_start))
    return render(request, "procurement/request_list.html", {
        "page_obj": page, "view": view, "q": q, "it": is_it_staff(request.user), "spend": spend,
        "pending_count": services.pending_for(request.user).count(), "year_start": year_start,
    })


@it_required
def request_edit(request, pk=None):
    req = _get(request, pk) if pk else None
    before = ProcurementRequest.objects.get(pk=pk) if pk else None
    if req and req.status not in services.EDITABLE_STATUSES:
        return _tracking_edit(request, req, before)
    if request.method == "POST":
        form = RequestForm(request.POST, instance=req)
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj = services.update_request(obj, before, request.user) if req else services.create_request(obj, request.user)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"{obj.reference} saved as {obj.get_status_display().lower()}. "
                                          "Attach a quotation and submit it when ready.")
                return redirect(obj)
    else:
        initial = {}
        for key in ("asset", "ticket", "project"):
            value = request.GET.get(key, "")
            if value.isdigit():
                initial[key] = int(value)
        form = RequestForm(instance=req, initial=initial or None)
    return render(request, "procurement/request_form.html", {"form": form, "req": req})


def _tracking_edit(request, req, before):
    if request.method == "POST":
        form = TrackingForm(request.POST, instance=req)
        if form.is_valid():
            services.update_request(form.save(commit=False), before, request.user)
            messages.success(request, f"{req.reference} updated.")
            return redirect(req)
    else:
        form = TrackingForm(instance=req)
    return render(request, "procurement/request_form.html", {"form": form, "req": req, "tracking": True})


@viewer_required
def request_detail(request, pk):
    req = _get(request, pk)
    history = AuditLog.objects.filter(content_type__model="procurementrequest", object_id=str(req.pk))
    return render(request, "procurement/request_detail.html", {
        "req": req, "it": is_it_staff(request.user), "can_approve": req.status == S.PENDING and services.can_approve(request.user, req),
        "approvals": req.approvals.select_related("approver"), "history": history,
        "attachments": req.attachments.filter(is_deleted=False), "attach_form": AttachmentForm(),
        "decision_form": DecisionForm(initial={"decision": "approved"}),
    })


@it_required
@require_POST
def request_submit(request, pk):
    req = _get(request, pk)
    try:
        req = services.submit(req, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Submitted for approval." if req.status == S.PENDING else
                         "Approved automatically under the configured approval rules.")
    return redirect(req)


@viewer_required
@require_POST
def request_decide(request, pk):
    req = _get(request, pk)
    form = DecisionForm(request.POST)
    if form.is_valid():
        try:
            services.decide(req, request.user, form.cleaned_data["decision"], comment=form.cleaned_data["comment"],
                            approved_amount=form.cleaned_data["approved_amount"])
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{req.reference} {form.cleaned_data['decision']}.")
    else:
        messages.error(request, "Choose approve or reject.")
    return redirect(req)


def _action(request, pk, form_class, handler, heading, button, success, initial=None):
    req = _get(request, pk)
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            try:
                handler(req, form.cleaned_data)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, success)
                return redirect(req)
    else:
        form = form_class(initial=initial(req) if initial else None)
    return render(request, "procurement/action.html", {"form": form, "req": req, "heading": heading, "button": button})


@it_required
def request_ordered(request, pk):
    return _action(request, pk, OrderedForm,
                   lambda r, d: services.mark_ordered(r, request.user, purchase_date=d["purchase_date"], supplier=d["supplier"]),
                   "Mark as ordered", "Mark ordered", "Marked as ordered.",
                   initial=lambda r: {"purchase_date": timezone.localdate(), "supplier": r.supplier_id})


@it_required
def request_delivered(request, pk):
    return _action(request, pk, DeliveredForm,
                   lambda r, d: services.mark_delivered(r, request.user, **d),
                   "Record delivery / completion", "Save", "Recorded as delivered.",
                   initial=lambda r: {"delivery_date": timezone.localdate(), "actual_expenditure": r.approved_cost,
                                      "warranty": r.warranty})


@it_required
def request_cancel(request, pk):
    return _action(request, pk, ReasonForm, lambda r, d: services.cancel(r, request.user, d["reason"]),
                   "Cancel request", "Cancel request", "Request cancelled.")


@it_required
@require_POST
def attach(request, pk):
    req = _get(request, pk)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid() and form.uploads:
        services.add_attachments(req, form.uploads, request.user, form.cleaned_data["doc_type"],
                                 form.cleaned_data["description"])
        messages.success(request, f"{len(form.uploads)} document(s) added.")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        if form.is_valid():
            messages.error(request, "Choose at least one file.")
    return redirect(req)


@login_required
def attachment_view(request, pk):
    att = get_object_or_404(ProcurementAttachment.objects.select_related("request"), pk=pk)
    _get(request, att.request_id)
    if att.is_deleted:
        raise PermissionDenied
    return serving.serve(request, att, download=request.GET.get("download") == "1")
