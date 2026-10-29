"""
Report context: period, filters and the viewer's permissions, plus filtered
querysets that every report section uses. Keeping the filtering here means all
sections of a report, and all export formats, count exactly the same records.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time

from django.utils import timezone

from apps.accounts.roles import Role, get_role


@dataclass
class ReportContext:
    kind: str
    start: date
    end: date
    user: object
    department_id: int | None = None
    task_category_id: int | None = None
    ticket_category_id: int | None = None
    priority: str | None = None
    status: str | None = None
    technician_id: int | None = None
    asset_id: int | None = None
    project_id: int | None = None
    activity_type: str | None = None
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.role = get_role(self.user)
        if self.role == Role.MANAGER:
            # Managers only ever see their own department's figures.
            self.department_id = self.user.profile.department_id or -1
        if self.start > self.end:
            self.start, self.end = self.end, self.start

    # -- period -------------------------------------------------------------
    @property
    def tz(self):
        return timezone.get_current_timezone()

    @property
    def start_dt(self):
        return timezone.make_aware(datetime.combine(self.start, time.min), self.tz)

    @property
    def end_dt(self):
        return timezone.make_aware(datetime.combine(self.end, time.max), self.tz)

    @property
    def is_manager(self):
        return self.role == Role.MANAGER

    @property
    def is_it(self):
        return self.role in (Role.ADMIN, Role.IT_OFFICER)

    def day_bounds(self, day):
        return (timezone.make_aware(datetime.combine(day, time.min), self.tz),
                timezone.make_aware(datetime.combine(day, time.max), self.tz))

    # -- filtered querysets -------------------------------------------------
    def tasks(self):
        from apps.tasks.models import Task

        qs = Task.objects.select_related("category", "assigned_to", "department", "project", "asset")
        if self.department_id:
            qs = qs.filter(department_id=self.department_id)
        if self.task_category_id:
            qs = qs.filter(category_id=self.task_category_id)
        if self.priority:
            qs = qs.filter(priority=self.priority)
        if self.technician_id:
            qs = qs.filter(assigned_to_id=self.technician_id)
        if self.asset_id:
            qs = qs.filter(asset_id=self.asset_id)
        if self.project_id:
            qs = qs.filter(project_id=self.project_id)
        if self.activity_type:
            qs = qs.filter(category__activity_type=self.activity_type)
        if self.status and self.status in Task.Status.values:
            qs = qs.filter(status=self.status)
        return qs

    def tickets(self):
        from django.db.models import Q

        from apps.helpdesk.models import Ticket

        qs = Ticket.objects.visible_to(self.user).select_related(
            "category", "subcategory", "department", "assigned_to", "requester", "asset")
        if self.department_id:
            qs = qs.filter(department_id=self.department_id)
        if self.ticket_category_id:
            qs = qs.filter(Q(category_id=self.ticket_category_id) | Q(subcategory_id=self.ticket_category_id))
        if self.priority:
            qs = qs.filter(priority=self.priority)
        if self.technician_id:
            qs = qs.filter(assigned_to_id=self.technician_id)
        if self.asset_id:
            qs = qs.filter(asset_id=self.asset_id)
        if self.project_id:
            qs = qs.filter(project_id=self.project_id)
        if self.status and self.status in Ticket.Status.values:
            qs = qs.filter(status=self.status)
        return qs

    def maintenance(self):
        from django.db.models import Q

        from apps.maintenance.models import MaintenanceRecord

        qs = MaintenanceRecord.objects.select_related("asset", "asset__category", "performed_by", "responsible")
        if self.department_id:
            qs = qs.filter(asset__department_id=self.department_id)
        if self.asset_id:
            qs = qs.filter(asset_id=self.asset_id)
        if self.project_id:
            qs = qs.filter(project_id=self.project_id)
        if self.technician_id:
            qs = qs.filter(Q(performed_by_id=self.technician_id) | Q(responsible_id=self.technician_id))
        return qs

    def assets(self):
        from apps.assets.models import Asset

        qs = Asset.objects.select_related("category", "department", "location")
        if self.department_id:
            qs = qs.filter(department_id=self.department_id)
        if self.asset_id:
            qs = qs.filter(pk=self.asset_id)
        return qs

    def procurement(self):
        from apps.procurement.models import ProcurementRequest

        qs = ProcurementRequest.objects.visible_to(self.user).select_related("asset", "project", "department", "supplier")
        if self.department_id:
            qs = qs.filter(department_id=self.department_id)
        if self.asset_id:
            qs = qs.filter(asset_id=self.asset_id)
        if self.project_id:
            qs = qs.filter(project_id=self.project_id)
        return qs

    def projects(self):
        from apps.projects.models import Project

        qs = Project.objects.visible_to(self.user).select_related("owner", "sponsor_department")
        if self.department_id:
            qs = qs.filter(sponsor_department_id=self.department_id)
        if self.project_id:
            qs = qs.filter(pk=self.project_id)
        return qs

    def time_records(self, start_dt=None, end_dt=None):
        """Counted time (the single once-only rule), with every report filter applied."""
        from django.contrib.auth import get_user_model

        from apps.diary.timecalc import time_records

        user = get_user_model().objects.filter(pk=self.technician_id).first() if self.technician_id else None
        records = time_records(start_dt or self.start_dt, end_dt or self.end_dt, user=user,
                               department_id=self.department_id, category_id=self.task_category_id,
                               activity_type=self.activity_type)
        if self.project_id:
            records = [r for r in records if r.project_id == self.project_id]
        if self.asset_id:
            records = [r for r in records if r.asset_id == self.asset_id]
        return records

    def filter_description(self):
        from django.contrib.auth import get_user_model

        from apps.accounts.models import Department
        from apps.assets.models import Asset
        from apps.diary.choices import ActivityType
        from apps.helpdesk.models import TicketCategory
        from apps.projects.models import Project
        from apps.tasks.models import TaskCategory

        parts = []
        if self.department_id and self.department_id > 0:
            parts.append(f"Department: {Department.objects.filter(pk=self.department_id).first()}")
        if self.task_category_id:
            parts.append(f"Task category: {TaskCategory.objects.filter(pk=self.task_category_id).first()}")
        if self.ticket_category_id:
            parts.append(f"Ticket category: {TicketCategory.objects.filter(pk=self.ticket_category_id).first()}")
        if self.priority:
            parts.append(f"Priority: {self.priority}")
        if self.status:
            parts.append(f"Status: {self.status.replace('_', ' ')}")
        if self.technician_id:
            u = get_user_model().objects.filter(pk=self.technician_id).first()
            parts.append(f"Technician: {u.get_full_name() or u.username}" if u else "Technician")
        if self.asset_id:
            parts.append(f"Asset: {Asset.objects.filter(pk=self.asset_id).first()}")
        if self.project_id:
            parts.append(f"Project: {Project.objects.filter(pk=self.project_id).first()}")
        if self.activity_type:
            parts.append(f"Activity: {dict(ActivityType.choices).get(self.activity_type, self.activity_type)}")
        return "; ".join(parts) or "No filters (all records you may see)"
