from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.diary.choices import ActivityType
from apps.evidence.models import EvidenceFile


class Priority(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


PRIORITY_RANK = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}


class TaskCategory(TimeStampedModel):
    """Administrator-managed task categories. Each maps to an activity type for time analysis."""

    name = models.CharField(max_length=80)
    activity_type = models.CharField(max_length=20, choices=ActivityType.choices, default=ActivityType.OTHER,
                                     help_text="Used to group recorded time in reports.")
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "task categories"
        constraints = [models.UniqueConstraint(Lower("name"), name="uniq_task_category_name_ci")]

    def __str__(self):
        return self.name


class TaskQuerySet(models.QuerySet):
    def open(self):
        return self.exclude(status__in=Task.CLOSED_STATUSES)

    def overdue(self, today=None):
        today = today or timezone.localdate()
        return self.open().filter(planned_date__lt=today)

    def visible_to(self, user):
        from apps.accounts.roles import is_it_staff

        return self if is_it_staff(user) else self.none()


class Task(TimeStampedModel):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        NOT_STARTED = "not_started", "Not started"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING_INFO = "waiting_info", "Waiting for information"
        WAITING_PARTS = "waiting_parts", "Waiting for parts / procurement"
        BLOCKED = "blocked", "Blocked"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    CLOSED_STATUSES = (Status.COMPLETED, Status.CANCELLED)
    # Moving into these statuses requires a reason (shown in daily reports).
    REASON_REQUIRED_STATUSES = (Status.WAITING_INFO, Status.WAITING_PARTS, Status.BLOCKED, Status.CANCELLED)
    # Statuses where the delay is outside the IT officer's control (fair metrics).
    EXTERNAL_WAIT_STATUSES = (Status.WAITING_INFO, Status.WAITING_PARTS)

    number = models.CharField(max_length=20, unique=True, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.ForeignKey(TaskCategory, on_delete=models.PROTECT, related_name="tasks")
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED, db_index=True)

    planned_date = models.DateField(db_index=True)
    original_planned_date = models.DateField(
        editable=False, help_text="The date first planned. Never changes, even when rescheduled."
    )
    scheduled_start = models.TimeField(null=True, blank=True)
    scheduled_end = models.TimeField(null=True, blank=True)
    actual_start = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    estimated_minutes = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(60 * 24 * 30)]
    )

    expected_outcome = models.TextField(blank=True)
    completion_notes = models.TextField(blank=True)
    delay_reason = models.TextField(blank=True, help_text="Latest reason for delay or non-completion.")
    follow_up_action = models.TextField(blank=True)

    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name="assigned_tasks")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="tasks")
    location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="tasks")

    # Unplanned / emergency work
    is_unplanned = models.BooleanField(default=False, db_index=True,
                                       help_text="Work that was not in the plan (emergency, walk-up request).")
    interrupted_task = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="interruptions",
                                         help_text="The planned task this work interrupted, if any.")

    asset = models.ForeignKey("assets.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.PROTECT, related_name="tasks")
    related_ticket = models.ForeignKey(
        "helpdesk.Ticket", null=True, blank=True, on_delete=models.PROTECT, related_name="tasks",
        help_text="When set, time worked on this task is recorded against the ticket.",
    )

    recurrence = models.ForeignKey("RecurringTask", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="occurrences")
    occurrence_date = models.DateField(null=True, blank=True, editable=False)

    objects = TaskQuerySet.as_manager()

    class Meta:
        ordering = ["planned_date", "scheduled_start", "id"]
        indexes = [
            models.Index(fields=["assigned_to", "planned_date"]),
            models.Index(fields=["status", "planned_date"]),
        ]
        constraints = [
            # A recurring schedule produces at most one task per date.
            models.UniqueConstraint(fields=["recurrence", "occurrence_date"], name="uniq_task_recurrence_date",
                                    condition=models.Q(recurrence__isnull=False)),
            models.CheckConstraint(
                condition=models.Q(scheduled_end__isnull=True) | models.Q(scheduled_start__isnull=True)
                | models.Q(scheduled_end__gt=models.F("scheduled_start")),
                name="task_scheduled_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.number} {self.title}"

    def get_absolute_url(self):
        return reverse("tasks:detail", args=[self.pk])

    # --- derived values ---------------------------------------------------
    @property
    def is_closed(self):
        return self.status in self.CLOSED_STATUSES

    @property
    def is_overdue(self):
        return not self.is_closed and self.planned_date < timezone.localdate()

    @property
    def completed_late(self):
        """Completed after the (latest) planned date, in the hotel's timezone."""
        if self.status != self.Status.COMPLETED or not self.completed_at:
            return False
        return timezone.localdate(self.completed_at) > self.planned_date

    @property
    def was_rescheduled(self):
        return self.planned_date != self.original_planned_date

    @property
    def recorded_minutes(self):
        """
        Time worked on this task (voided entries excluded). For a task linked to
        a ticket this is time stored on the ticket via this task — it is counted
        once, as ticket time, in all totals.
        """
        own = self.time_entries.filter(is_voided=False, ended_at__isnull=False).aggregate(
            total=models.Sum("minutes"))["total"] or 0
        via_ticket = self.ticket_time_entries.filter(is_voided=False, ended_at__isnull=False).aggregate(
            total=models.Sum("minutes"))["total"] or 0
        return own + via_ticket

    @property
    def priority_rank(self):
        return PRIORITY_RANK.get(self.priority, 9)

    def scheduled_start_dt(self):
        if not self.scheduled_start:
            return None
        return timezone.make_aware(datetime.combine(self.planned_date, self.scheduled_start))

    def clean(self):
        errors = {}
        if self.scheduled_start and self.scheduled_end and self.scheduled_end <= self.scheduled_start:
            errors["scheduled_end"] = "The end time must be after the start time."
        if self.assigned_to_id:
            from apps.accounts.roles import is_it_staff

            if not is_it_staff(self.assigned_to):
                errors["assigned_to"] = "Tasks can only be assigned to IT staff."
        if errors:
            raise ValidationError(errors)


class TaskComment(models.Model):
    """Timeline entry on a task: free comment or a recorded change (status, reschedule, interruption)."""

    class Kind(models.TextChoices):
        COMMENT = "comment", "Comment"
        STATUS = "status", "Status change"
        RESCHEDULE = "reschedule", "Rescheduled"
        INTERRUPTION = "interruption", "Interruption"
        SYSTEM = "system", "System"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.COMMENT)
    body = models.TextField()
    old_status = models.CharField(max_length=20, choices=Task.Status.choices, blank=True)
    new_status = models.CharField(max_length=20, choices=Task.Status.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.task.number} {self.get_kind_display()}"


class TaskAttachment(EvidenceFile):
    """Evidence on a task (IT staff only): photos of work done, configuration exports, reports."""

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="attachments")

    class Meta(EvidenceFile.Meta):
        pass


class TaskScheduleChange(models.Model):
    """Every reschedule, so daily reports can show work planned for a day but moved."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="schedule_changes")
    old_date = models.DateField(db_index=True)
    new_date = models.DateField()
    old_start = models.TimeField(null=True, blank=True)
    new_start = models.TimeField(null=True, blank=True)
    reason = models.TextField()
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at", "id"]

    def __str__(self):
        return f"{self.task.number}: {self.old_date} → {self.new_date}"


class TaskTimeEntry(models.Model):
    """
    Time worked on a task. An entry with ``ended_at`` empty is a running timer
    (at most one per user). Entries are corrected by voiding, never deleted.

    Time rule: once tickets exist (Phase 3), time on a task linked to a ticket
    is recorded against the ticket instead, so it is never counted twice.
    """

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="time_entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_time_entries")
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    minutes = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=300, blank=True)
    is_manual = models.BooleanField(default=False, help_text="Entered after the fact rather than timed.")

    is_voided = models.BooleanField(default=False, db_index=True)
    void_reason = models.CharField(max_length=300, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        verbose_name_plural = "task time entries"
        constraints = [
            models.UniqueConstraint(fields=["user"], condition=models.Q(ended_at__isnull=True, is_voided=False),
                                    name="one_running_timer_per_user"),
            models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gt=models.F("started_at")),
                                   name="time_entry_end_after_start"),
        ]

    def __str__(self):
        return f"{self.task.number} {self.minutes} min"

    @property
    def is_running(self):
        return self.ended_at is None and not self.is_voided


class RecurringTask(TimeStampedModel):
    """A schedule that generates ordinary tasks (e.g. daily internet check)."""

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        CUSTOM = "custom", "Every N days"

    WEEKDAYS = [("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"), ("4", "Fri"), ("5", "Sat"), ("6", "Sun")]

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    expected_outcome = models.TextField(blank=True)
    category = models.ForeignKey(TaskCategory, on_delete=models.PROTECT, related_name="recurring_tasks")
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="+")
    scheduled_start = models.TimeField(null=True, blank=True)
    scheduled_end = models.TimeField(null=True, blank=True)
    estimated_minutes = models.PositiveIntegerField(null=True, blank=True,
                                                    validators=[MinValueValidator(1), MaxValueValidator(60 * 24)])

    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.DAILY)
    interval = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(366)],
        help_text="Every N days / weeks / months.",
    )
    weekdays = models.CharField(max_length=20, blank=True, help_text="Weekly only: comma-separated 0=Mon … 6=Sun.")
    day_of_month = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Monthly only. 29–31 fall on the last day in shorter months.",
    )
    skip_weekends = models.BooleanField(default=False, help_text="Daily / every-N-days only.")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    generated_until = models.DateField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("tasks:recurring_edit", args=[self.pk])

    @property
    def weekday_list(self):
        return sorted({int(d) for d in self.weekdays.split(",") if d.strip().isdigit() and 0 <= int(d) <= 6})

    def describe(self):
        if self.frequency == self.Frequency.DAILY:
            text = "Every day" if self.interval == 1 else f"Every {self.interval} days"
            return text + (" (weekdays only)" if self.skip_weekends else "")
        if self.frequency == self.Frequency.CUSTOM:
            return f"Every {self.interval} days" + (" (weekdays only)" if self.skip_weekends else "")
        if self.frequency == self.Frequency.WEEKLY:
            names = ", ".join(dict(self.WEEKDAYS)[str(d)] for d in self.weekday_list) or "—"
            return (f"Every {self.interval} weeks" if self.interval > 1 else "Weekly") + f" on {names}"
        return (f"Every {self.interval} months" if self.interval > 1 else "Monthly") + f" on day {self.day_of_month}"

    def clean(self):
        errors = {}
        if self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "The end date must be on or after the start date."
        if self.frequency == self.Frequency.WEEKLY and not self.weekday_list:
            errors["weekdays"] = "Choose at least one weekday."
        if self.frequency == self.Frequency.MONTHLY and not self.day_of_month:
            errors["day_of_month"] = "Choose the day of the month."
        if self.scheduled_start and self.scheduled_end and self.scheduled_end <= self.scheduled_start:
            errors["scheduled_end"] = "The end time must be after the start time."
        if errors:
            raise ValidationError(errors)

    def occurs_on(self, day):
        """True if this schedule produces a task on ``day``."""
        if day < self.start_date or (self.end_date and day > self.end_date):
            return False
        delta = (day - self.start_date).days
        if self.frequency in (self.Frequency.DAILY, self.Frequency.CUSTOM):
            if self.skip_weekends and day.weekday() >= 5:
                return False
            return delta % self.interval == 0
        if self.frequency == self.Frequency.WEEKLY:
            week_index = (delta + self.start_date.weekday()) // 7
            return day.weekday() in self.weekday_list and week_index % self.interval == 0
        if self.frequency == self.Frequency.MONTHLY:
            months = (day.year - self.start_date.year) * 12 + day.month - self.start_date.month
            if months % self.interval:
                return False
            next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
            last_day = (next_month - timedelta(days=1)).day
            return day.day == min(self.day_of_month, last_day)
        return False
