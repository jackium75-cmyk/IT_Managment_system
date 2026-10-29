"""
Preventive and corrective maintenance rules.

* Each active plan has exactly one *scheduled* record (its next visit).
* ``lead_days`` before the due date a planner task is created for the
  responsible officer (time is recorded on that task) and a reminder is sent.
* Overdue visits trigger one reminder; after the configured grace period a
  visit is marked *missed* and the next one is scheduled from the missed due
  date, so the plan keeps its rhythm.
* Completing a visit schedules the next one from the completion date.
* Rescheduling keeps the original due date, so compliance stays honest.
"""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import is_it_staff
from apps.core import audit
from apps.core.models import AuditLog, SystemSetting
from apps.diary.models import ActivityLog
from apps.evidence.models import EvidenceFile
from apps.notifications.models import Notification
from apps.notifications.services import notify

from .models import MaintenanceAttachment, MaintenancePlan, MaintenanceRecord

R = MaintenanceRecord.Status
PREVENTIVE_CATEGORY = "Preventive maintenance"
CORRECTIVE_CATEGORY = "Corrective maintenance / repair"


def require_it(user):
    if not is_it_staff(user):
        raise PermissionDenied("Only IT staff can manage maintenance.")


def _diary(record, user, event, title, description="", outcome=""):
    if user is None or not is_it_staff(user):
        return
    from apps.diary.choices import ActivityType

    ActivityLog.objects.create(
        user=user, source=ActivityLog.Source.AUTO, event=event, activity_type=ActivityType.MAINTENANCE,
        occurred_at=timezone.now(), title=title[:200], description=description, outcome=outcome,
        department=record.asset.department, location=record.asset.location, asset=record.asset,
        task=record.task, ticket=record.ticket, project=record.project, created_by=user)


def _task_services():
    from apps.tasks import services

    return services


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
def ensure_record(plan):
    """Make sure an active plan has its next scheduled visit."""
    if not plan.is_active or not plan.asset.in_service:
        return None
    existing = plan.records.filter(status=R.SCHEDULED).first()
    if existing:
        return existing
    return MaintenanceRecord.objects.create(
        plan=plan, asset=plan.asset, kind=MaintenanceRecord.Kind.PREVENTIVE, title=plan.title,
        due_date=plan.next_due_date, original_due_date=plan.next_due_date, responsible=plan.responsible)


@transaction.atomic
def create_plan(plan, user):
    require_it(user)
    plan.created_by = user
    plan.full_clean()
    plan.save()
    record = ensure_record(plan)
    audit.log(AuditLog.Action.CREATE, plan.asset, actor=user,
              message=f"Maintenance plan created: {plan.title}, {plan.describe()}, first due {plan.next_due_date:%d %b %Y}")
    _maybe_create_task(record, timezone.localdate())
    return plan


@transaction.atomic
def update_plan(plan, before, user):
    require_it(user)
    plan.full_clean()
    plan.save()
    record = plan.records.filter(status=R.SCHEDULED).first()
    if not plan.is_active:
        if before.is_active:
            deactivate_plan(plan, user, "Plan switched off")
        return plan
    if record:
        record.title, record.responsible = plan.title, plan.responsible
        if record.due_date != plan.next_due_date:
            _move(record, plan.next_due_date, "Plan schedule changed", user)
        record.save()
    else:
        _maybe_create_task(ensure_record(plan), timezone.localdate())
    changes = audit.diff_instances(before, plan, ["title", "interval_value", "interval_unit", "responsible",
                                                  "next_due_date", "lead_days", "is_active"])
    if changes:
        audit.log(AuditLog.Action.UPDATE, plan.asset, actor=user, changes=changes,
                  message=f"Maintenance plan changed: {plan.title}")
    return plan


@transaction.atomic
def deactivate_plan(plan, user, reason):
    MaintenancePlan.objects.filter(pk=plan.pk).update(is_active=False)
    plan.is_active = False
    for record in plan.records.filter(status=R.SCHEDULED):
        cancel_record(record, user, reason, check_permission=False)
    audit.log(AuditLog.Action.DEACTIVATE, plan.asset, actor=user, message=f"Maintenance plan stopped: {plan.title}. {reason}")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def _maybe_create_task(record, today):
    """Create the planner task once the visit is within the plan's lead time."""
    if record is None or record.task_id or record.status != R.SCHEDULED or not record.plan_id:
        return None
    plan = record.plan
    if record.due_date - timedelta(days=plan.lead_days) > today:
        return None
    from apps.tasks.models import Task, TaskCategory

    category = TaskCategory.objects.filter(name=PREVENTIVE_CATEGORY).first() or TaskCategory.objects.filter(
        activity_type="maintenance").first()
    checklist = "\n".join(f"☐ {item}" for item in plan.checklist_items)
    task = Task(title=f"Maintenance: {plan.title} ({record.asset.tag})"[:200], category=category,
                planned_date=max(record.due_date, today), assigned_to=record.responsible or plan.responsible,
                asset=record.asset, department=record.asset.department, location=record.asset.location,
                estimated_minutes=plan.estimated_minutes, expected_outcome=checklist,
                description=f"Scheduled preventive maintenance. Complete the maintenance record: "
                            f"{reverse('maintenance:record', args=[record.pk])}")
    _task_services().create_task(task, plan.created_by, log=True)
    record.task = task
    record.save(update_fields=["task", "updated_at"])
    return task


def _move(record, new_date, reason, user):
    record.due_date = new_date
    record.reschedule_count += 1
    record.status_reason = reason
    record.reminded_on = None
    record.overdue_notified = False
    if record.task_id and not record.task.is_closed:
        try:
            _task_services().reschedule(record.task, user, new_date=max(new_date, timezone.localdate()),
                                        reason=f"Maintenance rescheduled: {reason}",
                                        new_start=record.task.scheduled_start, new_end=record.task.scheduled_end)
        except (ValidationError, PermissionDenied):
            pass


@transaction.atomic
def reschedule_record(record, user, new_date, reason):
    require_it(user)
    record = MaintenanceRecord.objects.select_for_update(of=("self",)).get(pk=record.pk)
    reason = (reason or "").strip()
    if record.status != R.SCHEDULED:
        raise ValidationError("Only scheduled maintenance can be rescheduled.")
    if not reason:
        raise ValidationError({"reason": "Give a reason for rescheduling."})
    if new_date == record.due_date:
        raise ValidationError({"new_date": "Choose a different date."})
    old = record.due_date
    _move(record, new_date, reason, user)
    record.save()
    if record.plan_id:
        MaintenancePlan.objects.filter(pk=record.plan_id).update(next_due_date=new_date)
    _diary(record, user, ActivityLog.Event.MAINTENANCE_STATUS, f"Rescheduled maintenance: {record}",
           f"{old:%d %b} → {new_date:%d %b %Y}. {reason}")
    audit.log(AuditLog.Action.UPDATE, record.asset, actor=user, changes={"maintenance_due": [str(old), str(new_date)]},
              message=f"Maintenance rescheduled: {record.title}. {reason}"[:500])
    _maybe_create_task(record, timezone.localdate())
    return record


@transaction.atomic
def cancel_record(record, user, reason, *, check_permission=True):
    if check_permission:
        require_it(user)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for cancelling."})
    record = MaintenanceRecord.objects.select_for_update(of=("self",)).get(pk=record.pk)
    if record.status != R.SCHEDULED:
        raise ValidationError("Only scheduled maintenance can be cancelled.")
    record.status, record.status_reason = R.CANCELLED, reason
    record.save()
    _close_task(record, user, reason)
    audit.log(AuditLog.Action.STATUS_CHANGE, record.asset, actor=user, message=f"Maintenance cancelled: {record.title}. {reason}")
    return record


def _close_task(record, user, reason):
    task = record.task
    if task and not task.is_closed:
        from apps.tasks.models import Task

        try:
            _task_services().change_status(task, Task.Status.CANCELLED, user or task.created_by, reason=reason)
        except (ValidationError, PermissionDenied):
            _task_services().change_status(task, Task.Status.CANCELLED, task.created_by, reason=reason)


@transaction.atomic
def complete_record(record, user, *, completed_on, checklist_results=(), work_done="", inspection_results="",
                    problems_found="", parts_used="", cost=None, currency="", recommendations="",
                    asset_condition=None, uploads=()):
    require_it(user)
    record = MaintenanceRecord.objects.select_for_update(of=("self",)).select_related("asset", "plan").get(pk=record.pk)
    if record.status != R.SCHEDULED:
        raise ValidationError("This maintenance is not open.")
    if completed_on > timezone.localdate():
        raise ValidationError({"completed_on": "The completion date cannot be in the future."})
    if not (work_done or "").strip():
        raise ValidationError({"work_done": "Describe the work done."})
    if any(r.get("result") == "issue" and not (r.get("note") or "").strip() for r in checklist_results):
        raise ValidationError({"checklist": "Add a note for every checklist item marked as an issue."})
    record.status, record.performed_by, record.completed_on = R.COMPLETED, user, completed_on
    record.checklist_results = list(checklist_results)
    record.work_done, record.inspection_results, record.problems_found = work_done.strip(), inspection_results, problems_found
    record.parts_used, record.recommendations = parts_used, recommendations
    record.cost = cost
    record.currency = (currency or (SystemSetting.get().currency if cost is not None else "")).upper()
    record.save()
    if uploads:
        add_attachments(record, uploads, user, stage=MaintenanceAttachment.Stage.AFTER)
    if asset_condition and asset_condition != record.asset.condition:
        from apps.assets import services as asset_services
        from apps.assets.models import Asset

        before = Asset.objects.get(pk=record.asset_id)
        record.asset.condition = asset_condition
        asset_services.update_asset(record.asset, before, user, note=f"Condition after maintenance {record.title}")
    if record.plan_id and record.plan.is_active:
        plan = record.plan
        plan.next_due_date = plan.following(completed_on)
        plan.save(update_fields=["next_due_date", "updated_at"])
        _maybe_create_task(ensure_record(plan), timezone.localdate())
    task = record.task
    if task and not task.is_closed:
        from apps.tasks.models import Task

        try:
            _task_services().change_status(task, Task.Status.COMPLETED, user,
                                           completion_notes=f"Maintenance record completed: {record.work_done}")
        except (ValidationError, PermissionDenied):
            pass
    summary = f"{len(record.issues)} issue(s) found" if record.issues else "No issues found"
    _diary(record, user, ActivityLog.Event.MAINTENANCE_DONE, f"Maintenance done: {record.title} ({record.asset.tag})",
           description=summary, outcome=record.work_done)
    audit.log(AuditLog.Action.STATUS_CHANGE, record.asset, actor=user,
              message=f"Maintenance completed: {record.title}" + (f", cost {record.cost} {record.currency}" if cost else ""))
    return record


@transaction.atomic
def record_corrective(asset, user, *, title, work_done, completed_on, problems_found="", parts_used="", cost=None,
                      currency="", recommendations="", ticket=None, asset_condition=None, asset_status=None,
                      uploads=()):
    """Record a repair that has been carried out (from the asset page or a ticket)."""
    require_it(user)
    record = MaintenanceRecord.objects.create(
        asset=asset, kind=MaintenanceRecord.Kind.CORRECTIVE, status=R.SCHEDULED, title=title,
        due_date=completed_on, original_due_date=completed_on, responsible=user, ticket=ticket)
    record = complete_record(record, user, completed_on=completed_on, work_done=work_done, problems_found=problems_found,
                             parts_used=parts_used, cost=cost, currency=currency, recommendations=recommendations,
                             asset_condition=asset_condition, uploads=uploads)
    if asset_status and asset_status != asset.status:
        from apps.assets import services as asset_services
        from apps.assets.models import Asset

        asset.refresh_from_db()
        before = Asset.objects.get(pk=asset.pk)
        asset.status = asset_status
        asset_services.update_asset(asset, before, user, note=f"Status after repair {record.title}")
    return record


@transaction.atomic
def add_attachments(record, uploads, user, stage=MaintenanceAttachment.Stage.OTHER, description=""):
    saved = []
    for f, _ext, kind, content_type in uploads:
        att = MaintenanceAttachment(record=record, stage=stage, original_name=f.name[:255], content_type=content_type,
                                    kind=kind, size=f.size, sha256=EvidenceFile.fingerprint(f),
                                    description=description[:255], uploaded_by=user)
        att.file.save(f.name, f, save=False)
        att.save()
        audit.log(AuditLog.Action.UPLOAD, record.asset, actor=user,
                  message=f"Maintenance evidence ({att.get_stage_display().lower()}): {att.original_name}")
        saved.append(att)
    return saved


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------
def run_jobs(today=None):
    """Create due tasks, send reminders, mark missed visits. Safe to run repeatedly."""
    today = today or timezone.localdate()
    grace = SystemSetting.get().maintenance_grace_days
    stats = {"tasks": 0, "reminders": 0, "missed": 0}
    for plan in MaintenancePlan.objects.filter(is_active=True).select_related("asset"):
        with transaction.atomic():
            ensure_record(plan)
    records = MaintenanceRecord.objects.filter(status=R.SCHEDULED, plan__isnull=False).select_related(
        "plan", "asset", "responsible", "task")
    for record in records:
        with transaction.atomic():
            if _maybe_create_task(record, today):
                stats["tasks"] += 1
            url = record.get_absolute_url()
            if record.due_date - timedelta(days=record.plan.lead_days) <= today and record.reminded_on is None:
                notify([record.responsible], Notification.Kind.MAINTENANCE,
                       f"Maintenance due {record.due_date:%d %b}: {record.title} ({record.asset.tag})", url=url,
                       dedupe_key=f"maint-due-{record.pk}-{record.due_date}", window_minutes=24 * 60)
                MaintenanceRecord.objects.filter(pk=record.pk).update(reminded_on=today)
                stats["reminders"] += 1
            if record.due_date < today and not record.overdue_notified:
                notify([record.responsible], Notification.Kind.OVERDUE,
                       f"Maintenance overdue: {record.title} ({record.asset.tag})", url=url,
                       dedupe_key=f"maint-overdue-{record.pk}", window_minutes=24 * 60)
                MaintenanceRecord.objects.filter(pk=record.pk).update(overdue_notified=True)
            if record.due_date + timedelta(days=grace) < today:
                _mark_missed(record, today)
                stats["missed"] += 1
    return stats


def _mark_missed(record, today):
    record.status = R.MISSED
    record.status_reason = f"Not completed within {SystemSetting.get().maintenance_grace_days} days of the due date"
    record.save()
    _close_task(record, None, "Maintenance window missed")
    plan = record.plan
    nxt = plan.following(record.due_date)
    while nxt <= today:  # skip windows that have also passed
        nxt = plan.following(nxt)
    plan.next_due_date = nxt
    plan.save(update_fields=["next_due_date", "updated_at"])
    ensure_record(plan)
    audit.log(AuditLog.Action.STATUS_CHANGE, record.asset, message=f"Maintenance missed: {record.title} (due {record.due_date})")
    notify([record.responsible], Notification.Kind.OVERDUE, f"Maintenance missed: {record.title} ({record.asset.tag})",
           body=f"Next visit scheduled for {nxt:%d %b %Y}.", url=record.get_absolute_url(),
           dedupe_key=f"maint-missed-{record.pk}")


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------
def compliance(start, end, qs=None):
    """
    Preventive maintenance compliance for visits originally due in [start, end].
    On time = completed on/before the ORIGINAL due date. Rate = on time ÷ (completed + missed);
    visits not yet due, or still within their window, are excluded from the rate.
    """
    qs = qs if qs is not None else MaintenanceRecord.objects.all()
    qs = qs.filter(kind__in=["preventive", "inspection"], original_due_date__range=(start, end)).exclude(status=R.CANCELLED)
    rows = list(qs.select_related("asset", "asset__category"))
    completed = [r for r in rows if r.status == R.COMPLETED]
    on_time = [r for r in completed if r.on_time]
    missed = [r for r in rows if r.status == R.MISSED]
    pending = [r for r in rows if r.status == R.SCHEDULED]
    decided = len(completed) + len(missed)
    return {
        "due": len(rows), "completed": len(completed), "on_time": len(on_time), "late": len(completed) - len(on_time),
        "missed": len(missed), "open": len(pending), "overdue": sum(1 for r in pending if r.is_overdue),
        "rescheduled": sum(1 for r in rows if r.reschedule_count),
        "rate": round(100 * len(on_time) / decided) if decided else None, "records": rows,
    }
