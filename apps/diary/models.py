from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .choices import ActivityType


class ActivityQuerySet(models.QuerySet):
    def current(self):
        return self.filter(is_voided=False)


class ActivityLog(models.Model):
    """
    One line in an IT officer's chronological activity diary.

    * ``AUTO`` entries are written by the system when something happens to a
      task, ticket, maintenance visit or project. They are
      immutable and never carry counted time themselves.
    * ``MANUAL`` entries are typed by the officer for work that did not come
      from a task or ticket (meetings, consultations, training ...). Their
      ``duration_minutes`` IS counted time. Edits are audited; entries are
      voided with a reason, never deleted.
    """

    class Source(models.TextChoices):
        AUTO = "auto", "Recorded by system"
        MANUAL = "manual", "Entered manually"

    class Event(models.TextChoices):
        TASK_CREATED = "task_created", "Task planned"
        TASK_STARTED = "task_started", "Work started"
        TASK_STATUS = "task_status", "Task status changed"
        TASK_COMPLETED = "task_completed", "Task completed"
        TASK_REOPENED = "task_reopened", "Task reopened"
        TASK_RESCHEDULED = "task_rescheduled", "Task rescheduled"
        TIME_LOGGED = "time_logged", "Time recorded"
        UNPLANNED = "unplanned", "Unplanned work recorded"
        TICKET_ACKNOWLEDGED = "ticket_acknowledged", "Ticket acknowledged"
        TICKET_STATUS = "ticket_status", "Ticket status changed"
        TICKET_TROUBLESHOOTING = "ticket_troubleshooting", "Troubleshooting step"
        TICKET_RESOLVED = "ticket_resolved", "Ticket resolved"
        TICKET_REOPENED = "ticket_reopened", "Ticket reopened"
        MAINTENANCE_DONE = "maintenance_done", "Maintenance completed"
        MAINTENANCE_STATUS = "maintenance_status", "Maintenance rescheduled / missed"
        ASSET_UPDATED = "asset_updated", "Asset updated"
        PROJECT_UPDATE = "project_update", "Project progress"
        PROCUREMENT = "procurement", "Procurement"
        KNOWLEDGE = "knowledge", "Knowledge article written"
        MANUAL = "manual", "Manual work entry"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="activities",
                             help_text="Whose diary this entry belongs to.")
    source = models.CharField(max_length=10, choices=Source.choices, db_index=True)
    event = models.CharField(max_length=30, choices=Event.choices, db_index=True)
    activity_type = models.CharField(max_length=20, choices=ActivityType.choices, db_index=True)
    occurred_at = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(24 * 60)],
        help_text="Only for manual entries. Counted in time totals.",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    outcome = models.TextField(blank=True)

    department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+")
    task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.PROTECT,
                             related_name="activities")
    time_entry = models.ForeignKey("tasks.TaskTimeEntry", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="activities")
    ticket = models.ForeignKey("helpdesk.Ticket", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="activities")
    asset = models.ForeignKey("assets.Asset", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="activities")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="activities")
    ticket_time_entry = models.ForeignKey("helpdesk.TicketTimeEntry", null=True, blank=True,
                                          on_delete=models.PROTECT, related_name="activities")

    is_voided = models.BooleanField(default=False, db_index=True)
    void_reason = models.CharField(max_length=300, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ActivityQuerySet.as_manager()

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [models.Index(fields=["user", "occurred_at"])]
        constraints = [
            # Counted time lives only on manual entries (auto entries point at
            # the TaskTimeEntry instead), so nothing is counted twice.
            models.CheckConstraint(
                condition=models.Q(source="manual") | models.Q(duration_minutes__isnull=True),
                name="diary_auto_entries_have_no_duration",
            ),
            models.CheckConstraint(
                condition=models.Q(source="auto") | models.Q(duration_minutes__isnull=False),
                name="diary_manual_entries_have_duration",
            ),
        ]

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.title}"

    @property
    def ends_at(self):
        return self.occurred_at + timedelta(minutes=self.duration_minutes) if self.duration_minutes else None

    @property
    def display_minutes(self):
        """Minutes to show next to the entry (counted for manual, informational for auto)."""
        if self.duration_minutes:
            return self.duration_minutes
        if self.time_entry_id:
            return self.time_entry.minutes
        if self.ticket_time_entry_id:
            return self.ticket_time_entry.minutes
        return None

    def clean(self):
        if self.source == self.Source.MANUAL and not self.duration_minutes:
            raise ValidationError({"duration_minutes": "Enter how long the activity took."})
