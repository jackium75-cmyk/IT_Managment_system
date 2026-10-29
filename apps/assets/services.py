"""Asset register business rules: tagging, moves, status changes, service history, failure analysis."""
import io
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import is_it_staff
from apps.core import audit
from apps.core.models import AuditLog, Sequence, SystemSetting
from apps.diary.choices import ActivityType
from apps.diary.models import ActivityLog
from apps.evidence.models import EvidenceFile

from .models import Asset, AssetAssignmentHistory, AssetAttachment

AUDIT_FIELDS = ["name", "category", "manufacturer", "model", "serial_number", "department", "location",
                "assigned_to", "purchase_date", "purchase_cost", "currency", "supplier", "warranty_expiry",
                "condition", "status", "ip_address", "mac_address", "hostname", "operating_system", "os_version",
                "replacement", "replacement_reason"]
MOVE_FIELDS = ("assigned_to", "department", "location")
OUT_OF_SERVICE = (Asset.Status.RETIRED, Asset.Status.DISPOSED, Asset.Status.LOST)
FAILURE_WINDOW_DAYS = 90
FAILURE_THRESHOLD = 3  # incidents + repairs in the window that flag "repeated failures"


def _diary(asset, user, title, description=""):
    if user is not None and is_it_staff(user):
        ActivityLog.objects.create(
            user=user, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.ASSET_UPDATED,
            activity_type=ActivityType.ADMINISTRATION, occurred_at=timezone.now(), title=title[:200],
            description=description, department=asset.department, location=asset.location, asset=asset,
            created_by=user)


def _record_move(asset, before, user, note=""):
    if before is not None and all(getattr(before, f + "_id") == getattr(asset, f + "_id") for f in MOVE_FIELDS):
        return None
    return AssetAssignmentHistory.objects.create(
        asset=asset, changed_by=user, note=note[:300],
        from_user=before.assigned_to if before else None, to_user=asset.assigned_to,
        from_department=before.department if before else None, to_department=asset.department,
        from_location=before.location if before else None, to_location=asset.location,
    )


@transaction.atomic
def create_asset(asset, user):
    code = asset.category.code
    asset.tag = f"{code}-{Sequence.next(code, 0):04d}"
    if asset.purchase_cost is not None and not asset.currency:
        asset.currency = SystemSetting.get().currency
    asset.full_clean()
    asset.save()
    if asset.assigned_to_id or asset.department_id or asset.location_id:
        _record_move(asset, None, user, "Registered")
    audit.log(AuditLog.Action.CREATE, asset, actor=user, message=f"Asset registered: {asset.category}")
    _diary(asset, user, f"Registered asset {asset}")
    return asset


@transaction.atomic
def update_asset(asset, before, user, note=""):
    if asset.status in OUT_OF_SERVICE and before.status not in OUT_OF_SERVICE:
        raise ValidationError({"status": "Use “Retire / dispose” so a reason is recorded."})
    if asset.purchase_cost is not None and not asset.currency:
        asset.currency = SystemSetting.get().currency
    asset.full_clean()
    asset.save()
    moved = _record_move(asset, before, user, note)
    changes = audit.diff_instances(before, asset, AUDIT_FIELDS)
    if changes:
        action = AuditLog.Action.STATUS_CHANGE if list(changes) == ["status"] else (
            AuditLog.Action.ASSIGN if moved and set(changes) <= set(MOVE_FIELDS) else AuditLog.Action.UPDATE)
        audit.log(action, asset, actor=user, changes=changes, message=note or "Asset details updated")
        _diary(asset, user, f"Updated asset {asset}", ", ".join(sorted(changes)))
    return asset


@transaction.atomic
def change_status(asset, status, user, reason):
    """Retire, dispose of, or report lost. Stops maintenance plans. Requires a reason."""
    reason = (reason or "").strip()
    if status not in Asset.Status.values:
        raise ValidationError({"status": "Choose a status."})
    if not reason:
        raise ValidationError({"reason": "Give a reason (e.g. 'beyond economical repair', 'replaced by PC-0042')."})
    asset = Asset.objects.select_for_update(of=("self",)).get(pk=asset.pk)
    old = asset.status
    if old == status:
        raise ValidationError({"status": "The asset already has this status."})
    asset.status = status
    asset.save(update_fields=["status", "updated_at"])
    AssetAssignmentHistory.objects.create(
        asset=asset, changed_by=user, note=f"Status: {Asset.Status(old).label} → {Asset.Status(status).label}. {reason}"[:300],
        from_user=asset.assigned_to, to_user=asset.assigned_to, from_department=asset.department,
        to_department=asset.department, from_location=asset.location, to_location=asset.location)
    audit.log(AuditLog.Action.STATUS_CHANGE, asset, actor=user, changes={"status": [old, status]}, message=reason)
    _diary(asset, user, f"{Asset.Status(status).label}: {asset}", reason)
    if status in OUT_OF_SERVICE:
        from apps.maintenance import services as maintenance

        for plan in asset.maintenance_plans.filter(is_active=True):
            maintenance.deactivate_plan(plan, user, f"Asset {Asset.Status(status).label.lower()}: {reason}")
    return asset


@transaction.atomic
def add_attachments(asset, uploads, user, description=""):
    saved = []
    for f, _ext, kind, content_type in uploads:
        att = AssetAttachment(asset=asset, original_name=f.name[:255], content_type=content_type, kind=kind,
                              size=f.size, sha256=EvidenceFile.fingerprint(f), description=description[:255],
                              uploaded_by=user)
        att.file.save(f.name, f, save=False)
        att.save()
        audit.log(AuditLog.Action.UPLOAD, asset, actor=user, message=f"Document uploaded: {att.original_name}")
        saved.append(att)
    return saved


@transaction.atomic
def hide_attachment(att, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for removing this file."})
    att.is_deleted, att.deleted_by, att.deleted_at, att.delete_reason = True, user, timezone.now(), reason[:300]
    att.save()
    audit.log(AuditLog.Action.DELETE, att.asset, actor=user,
              message=f"Document removed from view: {att.original_name}. Reason: {reason}"[:500])


# ---------------------------------------------------------------------------
# Service history and analysis
# ---------------------------------------------------------------------------
def service_history(asset):
    """Everything that happened to an asset, newest first, with any recorded expense."""
    events = []
    for t in asset.tickets.select_related("requester"):
        events.append({"at": t.created_at, "kind": "incident" if t.ticket_type == "incident" else "request",
                       "title": f"{t.reference} {t.subject}", "url": t.get_absolute_url(),
                       "detail": f"{t.get_status_display()} · reported by {t.requester.get_full_name() or t.requester.username}"
                                 + (f" · resolved: {t.resolution_details[:120]}" if t.resolution_details else "")})
    for r in asset.maintenance_records.exclude(status="scheduled").select_related("performed_by"):
        when = r.completed_on or r.due_date
        at = timezone.make_aware(datetime.combine(when, time(12))) if when else r.updated_at
        events.append({"at": at, "kind": "repair" if r.kind == "corrective" else "maintenance",
                       "title": f"{r.get_kind_display()}: {r.title}", "url": r.get_absolute_url(),
                       "detail": f"{r.get_status_display()}" + (f" · {r.work_done[:120]}" if r.work_done else "")
                                 + (f" · {r.status_reason[:120]}" if r.status_reason else ""),
                       "cost": r.cost, "currency": r.currency})
    for h in asset.assignment_history.select_related("to_user", "to_department", "to_location", "changed_by"):
        target = ", ".join(str(x) for x in (h.to_user and (h.to_user.get_full_name() or h.to_user.username),
                                            h.to_department, h.to_location) if x) or "unassigned"
        events.append({"at": h.changed_at, "kind": "move", "title": h.note or f"Assigned / moved to {target}",
                       "detail": f"{target} · by {h.changed_by.get_full_name() or h.changed_by.username}"})
    for p in asset.procurement_requests.exclude(status__in=["draft", "cancelled"]):
        events.append({"at": p.created_at, "kind": "purchase", "title": f"{p.reference} {p.item}",
                       "url": p.get_absolute_url(), "detail": f"{p.get_request_type_display()} · {p.get_status_display()}",
                       "cost": p.actual_expenditure, "currency": p.currency})
    events.sort(key=lambda e: e["at"], reverse=True)
    return events


def expenses(asset):
    """Recorded maintenance/repair costs and delivered purchases, per currency (never mixed or converted)."""
    totals = {}
    for r in asset.maintenance_records.filter(status="completed", cost__isnull=False):
        totals[r.currency or "?"] = totals.get(r.currency or "?", Decimal("0")) + r.cost
    for p in asset.procurement_requests.filter(status="delivered", actual_expenditure__isnull=False):
        totals[p.currency] = totals.get(p.currency, Decimal("0")) + p.actual_expenditure
    return totals


def failure_counts(since, until=None, qs=None):
    """Assets with incident tickets and repairs in the window: {asset_id: (incidents, repairs)}."""
    until = until or timezone.now()
    qs = qs if qs is not None else Asset.objects.all()
    rows = qs.annotate(
        incidents=Count("tickets", filter=Q(tickets__ticket_type="incident", tickets__created_at__range=(since, until)),
                        distinct=True),
        repairs=Count("maintenance_records", filter=Q(maintenance_records__kind="corrective",
                                                      maintenance_records__status="completed",
                                                      maintenance_records__completed_on__range=(since.date(), until.date())),
                      distinct=True),
    ).filter(Q(incidents__gt=0) | Q(repairs__gt=0))
    return rows


def attention_reasons(asset, failures=None):
    """Plain-language reasons this asset needs attention (empty list = fine)."""
    reasons = []
    if asset.status == Asset.Status.UNDER_REPAIR:
        reasons.append("under repair")
    if asset.condition in (Asset.Condition.POOR, Asset.Condition.BROKEN):
        reasons.append(f"condition: {asset.get_condition_display().lower()}")
    if asset.replacement in (Asset.Replacement.RECOMMENDED, Asset.Replacement.URGENT):
        reasons.append(asset.get_replacement_display().lower())
    if asset.in_service and asset.warranty_status == "expiring":
        reasons.append(f"warranty ends {asset.warranty_expiry:%d %b %Y}")
    if asset.in_service and asset.beyond_expected_life:
        reasons.append(f"{asset.age_years} years old (expected life {asset.category.expected_life_years})")
    if failures and failures >= FAILURE_THRESHOLD:
        reasons.append(f"{failures} failures in {FAILURE_WINDOW_DAYS} days")
    overdue = getattr(asset, "overdue_maintenance", None)
    if overdue:
        reasons.append("maintenance overdue")
    return reasons


def needing_attention(qs=None):
    """[(asset, reasons)] for in-service assets that need attention."""
    from apps.maintenance.models import MaintenanceRecord

    today = timezone.localdate()
    qs = (qs if qs is not None else Asset.objects.all()).in_service().select_related("category").annotate(
        overdue_maintenance=Count("maintenance_records", filter=Q(maintenance_records__status=MaintenanceRecord.Status.SCHEDULED,
                                                                  maintenance_records__due_date__lt=today)))
    since = timezone.now() - timedelta(days=FAILURE_WINDOW_DAYS)
    failures = {a.pk: a.incidents + a.repairs for a in failure_counts(since, qs=Asset.objects.all())}
    out = []
    for asset in qs:
        reasons = attention_reasons(asset, failures.get(asset.pk))
        if reasons:
            out.append((asset, reasons))
    return out


def scan_url(request, asset):
    from django.conf import settings

    path = reverse("assets:scan", args=[asset.qr_token])
    base = getattr(settings, "SITE_BASE_URL", "")
    return base.rstrip("/") + path if base else request.build_absolute_uri(path)


def qr_png(data, box_size=8):
    import qrcode

    img = qrcode.make(data, box_size=box_size, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
