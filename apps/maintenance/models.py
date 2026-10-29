from datetime import date, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.evidence.models import EvidenceFile


def add_interval(day, value, unit):
    if unit == MaintenancePlan.Unit.DAYS:
        return day + timedelta(days=value)
    if unit == MaintenancePlan.Unit.WEEKS:
        return day + timedelta(weeks=value)
    month = day.month - 1 + value
    year, month = day.year + month // 12, month % 12 + 1
    next_month = (date(year, month, 28) + timedelta(days=4)).replace(day=1)
    last = (next_month - timedelta(days=1)).day
    return day.replace(year=year, month=month, day=min(day.day, last))


class MaintenancePlan(TimeStampedModel):
    """A recurring preventive-maintenance schedule for one asset."""

    class Unit(models.TextChoices):
        DAYS = "days", "days"
        WEEKS = "weeks", "weeks"
        MONTHS = "months", "months"

    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="maintenance_plans")
    title = models.CharField(max_length=150, help_text="e.g. 'Quarterly server health check'.")
    interval_value = models.PositiveIntegerField("repeat every", default=3,
                                                 validators=[MinValueValidator(1), MaxValueValidator(120)])
    interval_unit = models.CharField(max_length=6, choices=Unit.choices, default=Unit.MONTHS)
    checklist = models.TextField(blank=True, help_text="One check per line. Each is ticked off when the work is done.")
    responsible = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    next_due_date = models.DateField(db_index=True)
    lead_days = models.PositiveIntegerField(
        default=7, validators=[MaxValueValidator(60)],
        help_text="Create the planner task and send a reminder this many days before the due date.")
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["next_due_date", "id"]

    def __str__(self):
        return f"{self.title} ({self.asset.tag})"

    @property
    def checklist_items(self):
        return [line.strip() for line in self.checklist.splitlines() if line.strip()]

    def describe(self):
        unit = self.get_interval_unit_display()
        if self.interval_value == 1:
            unit = unit.rstrip("s")
        return f"Every {self.interval_value} {unit}" if self.interval_value > 1 else f"Every {unit}"

    def following(self, day):
        return add_interval(day, self.interval_value, self.interval_unit)


class MaintenanceRecord(TimeStampedModel):
    """One maintenance occurrence: a scheduled preventive visit or a corrective repair."""

    class Kind(models.TextChoices):
        PREVENTIVE = "preventive", "Preventive"
        INSPECTION = "inspection", "Inspection"
        CORRECTIVE = "corrective", "Corrective / repair"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"
        MISSED = "missed", "Missed"
        CANCELLED = "cancelled", "Cancelled"

    plan = models.ForeignKey(MaintenancePlan, null=True, blank=True, on_delete=models.PROTECT, related_name="records")
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="maintenance_records")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PREVENTIVE, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    title = models.CharField(max_length=150)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    original_due_date = models.DateField(null=True, blank=True, editable=False)
    reschedule_count = models.PositiveIntegerField(default=0)
    responsible = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")

    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    completed_on = models.DateField(null=True, blank=True, db_index=True)
    checklist_results = models.JSONField(default=list, blank=True,
                                         help_text="[{item, result: ok|issue|na, note}]")
    inspection_results = models.TextField(blank=True)
    problems_found = models.TextField(blank=True)
    work_done = models.TextField(blank=True)
    parts_used = models.TextField(blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, blank=True)
    recommendations = models.TextField(blank=True)
    status_reason = models.TextField(blank=True, help_text="Why it was missed, cancelled or rescheduled.")

    task = models.OneToOneField("tasks.Task", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="maintenance_record")
    ticket = models.ForeignKey("helpdesk.Ticket", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="maintenance_records")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="maintenance_records")
    reminded_on = models.DateField(null=True, blank=True, editable=False)
    overdue_notified = models.BooleanField(default=False, editable=False)

    class Meta:
        ordering = ["-due_date", "-id"]
        constraints = [
            # A plan has at most one open (scheduled) occurrence at a time.
            models.UniqueConstraint(fields=["plan"], condition=models.Q(status="scheduled"),
                                    name="one_scheduled_record_per_plan"),
        ]

    def __str__(self):
        return f"{self.title} – {self.asset.tag}"

    def get_absolute_url(self):
        return reverse("maintenance:record", args=[self.pk])

    @property
    def is_overdue(self):
        return self.status == self.Status.SCHEDULED and self.due_date and self.due_date < timezone.localdate()

    @property
    def on_time(self):
        """Completed on or before the originally scheduled date (fair: reschedules don't hide lateness)."""
        if self.status != self.Status.COMPLETED or not self.completed_on:
            return None
        target = self.original_due_date or self.due_date
        return target is None or self.completed_on <= target

    @property
    def issues(self):
        return [r for r in self.checklist_results if r.get("result") == "issue"]


class MaintenanceAttachment(EvidenceFile):
    class Stage(models.TextChoices):
        BEFORE = "before", "Before"
        AFTER = "after", "After"
        OTHER = "other", "Other"

    record = models.ForeignKey(MaintenanceRecord, on_delete=models.PROTECT, related_name="attachments")
    stage = models.CharField(max_length=10, choices=Stage.choices, default=Stage.OTHER)

    class Meta(EvidenceFile.Meta):
        pass
