"""
Procurement workflow:

draft → (submit) → pending ──approve──▶ approved → ordered → delivered
                     │                     (auto-approved below the configured threshold)
                     └─reject──▶ rejected → (edit and resubmit) → pending
Any open request can be cancelled with a reason.

Approvers are administrators and the head of the request's department — never
the person who raised the request (segregation of duties).
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.roles import Role, get_role, is_it_staff
from apps.core import audit
from apps.core.models import AuditLog, Sequence, SystemSetting
from apps.diary.choices import ActivityType
from apps.diary.models import ActivityLog
from apps.evidence.models import EvidenceFile
from apps.notifications.models import Notification
from apps.notifications.services import admins, notify

from .models import Approval, ProcurementAttachment, ProcurementRequest

S = ProcurementRequest.Status
EDITABLE_STATUSES = (S.DRAFT, S.REJECTED)


def require_it(user):
    if not is_it_staff(user):
        raise PermissionDenied("Only IT staff can manage procurement requests.")


def _diary(req, user, title, description=""):
    if user is not None and is_it_staff(user):
        ActivityLog.objects.create(
            user=user, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.PROCUREMENT,
            activity_type=ActivityType.ADMINISTRATION, occurred_at=timezone.now(), title=title[:200],
            description=description, department=req.department, asset=req.asset, ticket=req.ticket,
            project=req.project, created_by=user)


def approvers_for(req):
    people = {u.pk: u for u in admins()}
    if req.department_id and req.department.head_id and req.department.head.is_active:
        people[req.department.head_id] = req.department.head
    people.pop(req.requested_by_id, None)
    return list(people.values())


def can_approve(user, req):
    if user.pk == req.requested_by_id:
        return False
    if get_role(user) == Role.ADMIN:
        return True
    return bool(req.department_id and req.department.head_id == user.pk)


def pending_for(user):
    """Requests waiting for this user's decision."""
    qs = ProcurementRequest.objects.filter(status=S.PENDING).exclude(requested_by=user)
    if get_role(user) == Role.ADMIN:
        return qs
    return qs.filter(department__head=user)


@transaction.atomic
def create_request(req, user):
    require_it(user)
    year = timezone.localdate().year
    req.reference = f"PR-{year}-{Sequence.next('PR', year):04d}"
    req.requested_by = user
    req.currency = (req.currency or SystemSetting.get().currency).upper()
    req.status = S.DRAFT
    req.full_clean()
    req.save()
    audit.log(AuditLog.Action.CREATE, req, actor=user, message=f"Procurement request created: {req.item}")
    return req


@transaction.atomic
def update_request(req, before, user):
    require_it(user)
    if before.status not in EDITABLE_STATUSES:
        for field in ("item", "quantity", "reason", "estimated_cost", "currency", "request_type", "department"):
            if getattr(before, field) != getattr(req, field):
                raise ValidationError(f"“{req._meta.get_field(field).verbose_name}” cannot change after submission. "
                                      "Cancel and raise a new request instead.")
    req.full_clean()
    req.save()
    changes = audit.diff_instances(before, req, ["item", "quantity", "estimated_cost", "currency", "supplier",
                                                 "purchase_date", "delivery_date", "actual_expenditure", "warranty",
                                                 "asset", "ticket", "project"])
    if changes:
        audit.log(AuditLog.Action.UPDATE, req, actor=user, changes=changes, message="Procurement request updated")
    return req


@transaction.atomic
def submit(req, user):
    require_it(user)
    req = ProcurementRequest.objects.select_for_update(of=("self",)).get(pk=req.pk)
    if req.status not in EDITABLE_STATUSES:
        raise ValidationError("Only draft or rejected requests can be submitted.")
    cfg = SystemSetting.get()
    req.submitted_at = timezone.now()
    req.requires_approval = cfg.procurement_requires_approval and req.estimated_cost >= cfg.procurement_approval_threshold
    if req.requires_approval:
        req.status = S.PENDING
        req.save()
        people = approvers_for(req)
        if not people:
            raise ValidationError("No one can approve this request: set a department head or an administrator "
                                  "who is not the requester.")
        notify(people, Notification.Kind.APPROVAL, f"Approval needed: {req.reference} {req.item}",
               body=f"{req.estimated_cost} {req.currency} · {req.reason[:200]}", url=req.get_absolute_url(),
               dedupe_key=f"approval-{req.pk}-{req.submitted_at:%Y%m%d%H%M%S}")
        message = "Submitted for approval"
    else:
        req.status = S.APPROVED
        req.approved_cost = req.estimated_cost
        req.save()
        message = ("Approved automatically (approval not required)" if not cfg.procurement_requires_approval else
                   f"Approved automatically (below the {cfg.procurement_approval_threshold} {cfg.currency} threshold)")
    audit.log(AuditLog.Action.STATUS_CHANGE, req, actor=user, changes={"status": [S.DRAFT, req.status]}, message=message)
    _diary(req, user, f"{message}: {req.reference} {req.item}")
    return req


@transaction.atomic
def decide(req, user, decision, *, comment="", approved_amount=None):
    req = ProcurementRequest.objects.select_for_update(of=("self",)).select_related("department").get(pk=req.pk)
    if req.status != S.PENDING:
        raise ValidationError("This request is not awaiting approval.")
    if not can_approve(user, req):
        audit.log(AuditLog.Action.ACCESS_DENIED, req, actor=user, message="Attempted to approve without authority")
        raise PermissionDenied("You cannot approve this request." if user.pk != req.requested_by_id
                               else "You cannot approve your own request.")
    comment = (comment or "").strip()
    if decision == Approval.Decision.REJECTED and not comment:
        raise ValidationError({"comment": "Give a reason for rejecting the request."})
    if decision == Approval.Decision.APPROVED:
        amount = approved_amount if approved_amount is not None else req.estimated_cost
        req.status, req.approved_cost = S.APPROVED, amount
    elif decision == Approval.Decision.REJECTED:
        amount = None
        req.status = S.REJECTED
    else:
        raise ValidationError("Unknown decision.")
    req.save()
    Approval.objects.create(request=req, approver=user, decision=decision, comment=comment, approved_amount=amount)
    audit.log(AuditLog.Action.APPROVAL, req, actor=user, changes={"status": [S.PENDING, req.status]},
              message=f"{decision.capitalize()}" + (f": {comment}" if comment else ""))
    notify([req.requested_by], Notification.Kind.APPROVAL,
           f"{req.reference} {decision}: {req.item}", body=comment, url=req.get_absolute_url(), exclude=user)
    return req


@transaction.atomic
def mark_ordered(req, user, *, purchase_date=None, supplier=None):
    require_it(user)
    req = ProcurementRequest.objects.select_for_update(of=("self",)).get(pk=req.pk)
    if req.status != S.APPROVED:
        raise ValidationError("Only approved requests can be marked as ordered.")
    req.status = S.ORDERED
    req.purchase_date = purchase_date or req.purchase_date or timezone.localdate()
    req.supplier = supplier or req.supplier
    req.save()
    audit.log(AuditLog.Action.STATUS_CHANGE, req, actor=user, changes={"status": [S.APPROVED, S.ORDERED]})
    _diary(req, user, f"Ordered {req.reference}: {req.item}")
    return req


@transaction.atomic
def mark_delivered(req, user, *, actual_expenditure, delivery_date, outcome="", warranty="", cost_saving_note=""):
    require_it(user)
    req = ProcurementRequest.objects.select_for_update(of=("self",)).get(pk=req.pk)
    if req.status not in (S.APPROVED, S.ORDERED):
        raise ValidationError("Only approved or ordered requests can be completed.")
    if actual_expenditure is None:
        raise ValidationError({"actual_expenditure": "Record the actual amount spent (0 if free / under warranty)."})
    old = req.status
    req.status, req.actual_expenditure, req.delivery_date = S.DELIVERED, actual_expenditure, delivery_date
    req.purchase_date = req.purchase_date or delivery_date
    req.outcome, req.warranty, req.cost_saving_note = outcome, warranty or req.warranty, cost_saving_note
    req.full_clean()
    req.save()
    audit.log(AuditLog.Action.STATUS_CHANGE, req, actor=user, changes={"status": [old, S.DELIVERED]},
              message=f"Delivered; actual expenditure {actual_expenditure} {req.currency}")
    _diary(req, user, f"Received {req.reference}: {req.item}", outcome)
    return req


@transaction.atomic
def cancel(req, user, reason):
    require_it(user)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for cancelling."})
    req = ProcurementRequest.objects.select_for_update(of=("self",)).get(pk=req.pk)
    if not req.is_open and req.status != S.REJECTED:
        raise ValidationError("This request is already closed.")
    old = req.status
    req.status = S.CANCELLED
    req.outcome = (req.outcome + "\n" if req.outcome else "") + f"Cancelled: {reason}"
    req.save()
    audit.log(AuditLog.Action.STATUS_CHANGE, req, actor=user, changes={"status": [old, S.CANCELLED]}, message=reason)
    return req


@transaction.atomic
def add_attachments(req, uploads, user, doc_type, description=""):
    require_it(user)
    for f, _ext, kind, content_type in uploads:
        att = ProcurementAttachment(request=req, doc_type=doc_type, original_name=f.name[:255], content_type=content_type,
                                    kind=kind, size=f.size, sha256=EvidenceFile.fingerprint(f),
                                    description=description[:255], uploaded_by=user)
        att.file.save(f.name, f, save=False)
        att.save()
        audit.log(AuditLog.Action.UPLOAD, req, actor=user, message=f"{att.get_doc_type_display()} uploaded: {att.original_name}")


def spend_by_currency(qs):
    """Delivered expenditure per currency for a queryset of requests."""
    totals = {}
    for cur, amount in qs.filter(status=S.DELIVERED, actual_expenditure__isnull=False).values_list(
            "currency", "actual_expenditure"):
        totals[cur] = totals.get(cur, 0) + amount
    return totals


def visible_q(user):
    return Q(pk__in=ProcurementRequest.objects.visible_to(user).values("pk"))
