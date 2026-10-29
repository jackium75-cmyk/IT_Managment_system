"""Project rules: numbering, status changes, progress log, linked work, time and cost roll-ups."""
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.accounts.roles import is_it_staff
from apps.core import audit
from apps.core.models import AuditLog, Sequence, SystemSetting
from apps.diary.choices import ActivityType
from apps.diary.models import ActivityLog
from apps.evidence.models import EvidenceFile

from .models import Project, ProjectAttachment, ProjectUpdate

P = Project.Status
AUDIT_FIELDS = ["name", "status", "priority", "owner", "sponsor_department", "start_date", "target_date", "completed_on",
                "estimated_cost", "other_costs", "currency", "documented_savings", "benefits_verified_by"]


BENEFIT_FIELDS = ["benefits", "documented_savings", "savings_evidence", "final_outcome"]


def require_it(user):
    if not is_it_staff(user):
        raise PermissionDenied("Only IT staff can change projects.")


def can_view(user, project):
    return Project.objects.visible_to(user).filter(pk=project.pk).exists()


def _log(project, user, body, *, system=False):
    update = ProjectUpdate.objects.create(project=project, author=user, body=body, status_snapshot=project.status,
                                          progress_snapshot=project.progress, is_system=system)
    if user is not None and is_it_staff(user):
        ActivityLog.objects.create(
            user=user, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.PROJECT_UPDATE,
            activity_type=ActivityType.PROJECT, occurred_at=timezone.now(),
            title=f"{project.code} {project.name}: {body}"[:200], description=body, project=project,
            department=project.sponsor_department, created_by=user)
    return update


@transaction.atomic
def create_project(project, user):
    require_it(user)
    year = timezone.localdate().year
    project.code = f"PRJ-{year}-{Sequence.next('PRJ', year):03d}"
    project.created_by = user
    if (project.estimated_cost is not None or project.other_costs is not None) and not project.currency:
        project.currency = SystemSetting.get().currency
    project.full_clean()
    project.save()
    _log(project, user, "Project created", system=True)
    audit.log(AuditLog.Action.CREATE, project, actor=user, message=f"Project created: {project.name}")
    return project


@transaction.atomic
def update_project(project, before, user):
    require_it(user)
    if project.status == P.COMPLETED and before.status != P.COMPLETED:
        if not project.final_outcome.strip():
            raise ValidationError({"final_outcome": "Describe the final outcome before marking the project completed."})
        project.completed_on = project.completed_on or timezone.localdate()
    if project.status != P.COMPLETED and before.status == P.COMPLETED:
        project.completed_on = None
    if (project.estimated_cost is not None or project.other_costs is not None) and not project.currency:
        project.currency = SystemSetting.get().currency
    confirmed_text_changed = before.benefits_verified_by_id and any(
        getattr(before, f) != getattr(project, f) for f in BENEFIT_FIELDS)
    if confirmed_text_changed:
        project.benefits_verified_by, project.benefits_verified_at = None, None
    project.full_clean()
    project.save()
    if confirmed_text_changed:
        _log(project, user, "Benefits changed after the manager's confirmation; confirmation withdrawn until they "
                            "confirm again.", system=True)
    changes = audit.diff_instances(before, project, AUDIT_FIELDS)
    if "status" in changes:
        _log(project, user, f"Status: {P(before.status).label} → {P(project.status).label}", system=True)
    if changes:
        audit.log(AuditLog.Action.STATUS_CHANGE if list(changes) == ["status"] else AuditLog.Action.UPDATE, project,
                  actor=user, changes=changes, message="Project updated")
    return project


def can_confirm_benefits(user, project):
    """Benefits are *verified* only when a manager of the sponsoring department confirms them personally.

    IT staff and administrators cannot confirm on someone's behalf: that would not be independent.
    """
    from apps.accounts.roles import Role, get_role

    return (project.status == P.COMPLETED and bool(project.benefits.strip()) and project.sponsor_department_id
            and get_role(user) == Role.MANAGER and user.profile.department_id == project.sponsor_department_id
            and not user.is_superuser)


@transaction.atomic
def confirm_benefits(project, user):
    project = Project.objects.select_for_update(of=("self",)).get(pk=project.pk)
    if not can_confirm_benefits(user, project):
        raise PermissionDenied("Only a manager of the sponsoring department can confirm the benefits of a completed "
                               "project.")
    if project.benefits_verified_by_id:
        raise ValidationError("The benefits have already been confirmed.")
    project.benefits_verified_by, project.benefits_verified_at = user, timezone.now()
    project.save(update_fields=["benefits_verified_by", "benefits_verified_at", "updated_at"])
    _log(project, user, f"Benefits confirmed by {user.get_full_name() or user.username}", system=True)
    audit.log(AuditLog.Action.APPROVAL, project, actor=user, message="Project benefits confirmed by manager")
    return project


@transaction.atomic
def add_update(project, user, body):
    require_it(user)
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Write the progress update."})
    return _log(project, user, body)


@transaction.atomic
def save_milestone(milestone, user):
    require_it(user)
    milestone.full_clean()
    created = milestone.pk is None
    milestone.save()
    if created:
        _log(milestone.project, user, f"Milestone added: {milestone.title}", system=True)
    return milestone


@transaction.atomic
def toggle_milestone(milestone, user):
    require_it(user)
    if milestone.completed_on:
        milestone.completed_on = None
        msg = f"Milestone reopened: {milestone.title}"
    else:
        milestone.completed_on = timezone.localdate()
        msg = f"Milestone completed: {milestone.title}"
    milestone.save(update_fields=["completed_on"])
    _log(milestone.project, user, msg, system=True)
    audit.log(AuditLog.Action.UPDATE, milestone.project, actor=user, message=msg)
    return milestone


@transaction.atomic
def add_attachments(project, uploads, user, description=""):
    require_it(user)
    for f, _ext, kind, content_type in uploads:
        att = ProjectAttachment(project=project, original_name=f.name[:255], content_type=content_type, kind=kind,
                                size=f.size, sha256=EvidenceFile.fingerprint(f), description=description[:255],
                                uploaded_by=user)
        att.file.save(f.name, f, save=False)
        att.save()
        audit.log(AuditLog.Action.UPLOAD, project, actor=user, message=f"Document uploaded: {att.original_name}")


# ---------------------------------------------------------------------------
# Roll-ups
# ---------------------------------------------------------------------------
def time_minutes(project):
    """
    Time recorded on the project's work, using the same once-only rule as all
    reports: task entries of the project's tasks, plus ticket entries of tickets
    in the project or recorded via the project's tasks (each entry counted once).
    """
    from apps.helpdesk.models import TicketTimeEntry
    from apps.tasks.models import TaskTimeEntry

    tasks = TaskTimeEntry.objects.filter(task__project=project, is_voided=False, ended_at__isnull=False) \
        .aggregate(m=Sum("minutes"))["m"] or 0
    tickets = TicketTimeEntry.objects.filter(Q(ticket__project=project) | Q(via_task__project=project),
                                             is_voided=False, ended_at__isnull=False).distinct() \
        .aggregate(m=Sum("minutes"))["m"] or 0
    return tasks + tickets


def costs(project):
    """
    {currency: {"estimated", "approved", "actual", "other"}} — kept per currency,
    never converted. Actual = delivered procurement expenditure + other recorded costs.
    """
    from apps.procurement.models import ProcurementRequest

    out = {}

    def row(cur):
        return out.setdefault(cur or "?", {"estimated": None, "approved": Decimal("0"), "actual": Decimal("0"),
                                           "other": Decimal("0")})

    if project.estimated_cost is not None:
        row(project.currency)["estimated"] = project.estimated_cost
    if project.other_costs:
        row(project.currency)["other"] += project.other_costs
        row(project.currency)["actual"] += project.other_costs
    for pr in project.procurement_requests.exclude(status__in=[ProcurementRequest.Status.REJECTED,
                                                               ProcurementRequest.Status.CANCELLED]):
        r = row(pr.currency)
        if pr.approved_cost is not None:
            r["approved"] += pr.approved_cost
        if pr.actual_expenditure is not None:
            r["actual"] += pr.actual_expenditure
    return out


def linked_work(project):
    from apps.maintenance.models import MaintenanceRecord

    return {
        "tasks": project.tasks.select_related("assigned_to").order_by("planned_date"),
        "tickets": project.tickets.select_related("requester").order_by("-created_at"),
        "maintenance": MaintenanceRecord.objects.filter(project=project).select_related("asset"),
        "procurement": project.procurement_requests.order_by("-created_at"),
    }
