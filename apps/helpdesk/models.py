from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.evidence.models import EvidenceFile
from apps.tasks.models import Priority


class TicketCategory(TimeStampedModel):
    """Two-level category tree: top-level categories and their subcategories."""

    name = models.CharField(max_length=80)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    description = models.CharField(max_length=255, blank=True,
                                   help_text="Shown to staff to help them choose (e.g. 'printing, scanning, toner').")
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "ticket categories"
        constraints = [models.UniqueConstraint(Lower("name"), "parent", name="uniq_ticket_category_name_parent")]

    def __str__(self):
        return f"{self.parent.name} › {self.name}" if self.parent_id else self.name


class SLATarget(models.Model):
    """Response and resolution targets per priority (calendar time: hotels run 24/7)."""

    priority = models.CharField(max_length=10, choices=Priority.choices, unique=True)
    response_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1)],
                                                   help_text="First response from IT within …")
    resolution_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1)],
                                                     help_text="Resolved within … (time waiting on others is excluded)")

    class Meta:
        verbose_name = "service target"

    def __str__(self):
        return f"{self.get_priority_display()}: respond {self.response_minutes} min, resolve {self.resolution_minutes} min"


class TicketQuerySet(models.QuerySet):
    def open(self):
        return self.exclude(status__in=(Ticket.Status.RESOLVED, Ticket.Status.CLOSED))

    def visible_to(self, user):
        from apps.accounts.roles import Role, get_role

        role = get_role(user)
        if role in (Role.ADMIN, Role.IT_OFFICER):
            return self
        if role == Role.MANAGER:
            dept = user.profile.department_id
            return self.filter(models.Q(requester=user) | models.Q(department_id=dept)) if dept else self.filter(requester=user)
        return self.filter(requester=user)

    def overdue(self, now=None):
        now = now or timezone.now()
        return self.open().exclude(status=Ticket.Status.WAITING).filter(resolution_due_at__lt=now)


class Ticket(TimeStampedModel):
    class Type(models.TextChoices):
        INCIDENT = "incident", "Incident (something is not working)"
        REQUEST = "request", "Service request (I need something)"

    class Status(models.TextChoices):
        NEW = "new", "New"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting", "Waiting"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    class WaitingOn(models.TextChoices):
        REQUESTER = "requester", "Requester (more information)"
        VENDOR = "vendor", "Vendor / supplier"
        PARTS = "parts", "Parts / procurement"
        APPROVAL = "approval", "Management approval"
        OTHER = "other", "Other third party"

    class Impact(models.TextChoices):
        ONE = "one", "Only me"
        SEVERAL = "several", "Several people / my department"
        GUESTS = "guests", "Guests or the whole hotel"

    class Urgency(models.TextChoices):
        MINOR = "minor", "Minor – I can work normally"
        SLOWED = "slowed", "My work is slowed down"
        STOPPED = "stopped", "I cannot work / service has stopped"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    ticket_type = models.CharField("type", max_length=10, choices=Type.choices, default=Type.INCIDENT, db_index=True)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    category = models.ForeignKey(TicketCategory, on_delete=models.PROTECT, related_name="tickets",
                                 limit_choices_to={"parent__isnull": True})
    subcategory = models.ForeignKey(TicketCategory, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="sub_tickets")

    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets")
    contact_phone = models.CharField(max_length=40, blank=True)
    contact_email = models.EmailField(blank=True)
    department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="tickets")
    location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="tickets")
    location_detail = models.CharField("exact place", max_length=150, blank=True,
                                       help_text="Room number, desk, or anything that helps find it.")
    affected_device = models.CharField(max_length=150, blank=True,
                                       help_text="e.g. 'Reception printer', 'POS 2', 'my laptop'.")

    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="tickets")
    asset = models.ForeignKey("assets.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="tickets",
                              help_text="The registered equipment this ticket is about.")

    impact = models.CharField(max_length=10, choices=Impact.choices, default=Impact.ONE)
    urgency = models.CharField(max_length=10, choices=Urgency.choices, default=Urgency.SLOWED)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM, db_index=True)
    priority_overridden = models.BooleanField(default=False)

    status = models.CharField(max_length=15, choices=Status.choices, default=Status.NEW, db_index=True)
    waiting_on = models.CharField(max_length=10, choices=WaitingOn.choices, blank=True)
    waiting_since = models.DateTimeField(null=True, blank=True)
    paused_minutes = models.PositiveIntegerField(default=0, help_text="Total time spent waiting on others.")

    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="assigned_tickets")

    response_due_at = models.DateTimeField(null=True, blank=True)
    resolution_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    resolution_details = models.TextField(blank=True)
    root_cause = models.TextField(blank=True)
    verification = models.TextField("verification / testing done", blank=True)

    feedback_rating = models.PositiveSmallIntegerField(null=True, blank=True,
                                                       validators=[MinValueValidator(1), MaxValueValidator(5)])
    feedback_comment = models.TextField(blank=True)
    confirmed_by_requester = models.BooleanField(default=False)
    reopen_count = models.PositiveIntegerField(default=0)

    objects = TicketQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "priority"]), models.Index(fields=["requester", "status"])]

    def __str__(self):
        return f"{self.reference} {self.subject}"

    def get_absolute_url(self):
        return reverse("helpdesk:detail", args=[self.pk])

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.subcategory_id and self.category_id and self.subcategory.parent_id != self.category_id:
            raise ValidationError({"subcategory": "This subcategory does not belong to the chosen category."})

    @property
    def reported_at(self):
        return self.created_at

    @property
    def is_open(self):
        return self.status not in (self.Status.RESOLVED, self.Status.CLOSED)

    def _now_paused(self, now=None):
        if self.status == self.Status.WAITING and self.waiting_since and self.waiting_on != "":
            return max(0, int(((now or timezone.now()) - self.waiting_since).total_seconds() // 60))
        return 0

    @property
    def effective_resolution_due(self):
        """Resolution target, shifted by time spent waiting on others (including now)."""
        if not self.resolution_due_at:
            return None
        return self.resolution_due_at + timedelta(minutes=self._now_paused())

    @property
    def is_overdue(self):
        due = self.effective_resolution_due
        return self.is_open and self.status != self.Status.WAITING and due is not None and timezone.now() > due

    @property
    def response_breached(self):
        if not self.response_due_at:
            return False
        responded = self.first_response_at or (timezone.now() if self.is_open else None)
        return responded is not None and responded > self.response_due_at

    @property
    def resolution_breached(self):
        if not self.resolution_due_at:
            return False
        if self.resolved_at:
            return self.resolved_at > self.resolution_due_at
        return self.is_overdue

    @property
    def resolution_minutes(self):
        """Elapsed minutes from report to resolution, excluding time waiting on others."""
        if not self.resolved_at:
            return None
        return max(0, int((self.resolved_at - self.created_at).total_seconds() // 60) - self.paused_minutes)

    @property
    def recorded_minutes(self):
        return self.time_entries.filter(is_voided=False, ended_at__isnull=False).aggregate(
            t=models.Sum("minutes"))["t"] or 0


class TicketComment(models.Model):
    class Kind(models.TextChoices):
        PUBLIC = "public", "Reply (visible to requester)"
        INTERNAL = "internal", "Internal note"
        TROUBLESHOOTING = "troubleshooting", "Troubleshooting step"
        SYSTEM = "system", "System"

    INTERNAL_KINDS = (Kind.INTERNAL, Kind.TROUBLESHOOTING)

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PUBLIC)
    body = models.TextField()
    is_question = models.BooleanField(default=False, help_text="IT asked the requester for information.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.ticket.reference} {self.get_kind_display()}"

    @property
    def is_internal(self):
        return self.kind in self.INTERNAL_KINDS


class TicketAttachment(EvidenceFile):
    class Stage(models.TextChoices):
        REPORTED = "reported", "When reported"
        TROUBLESHOOTING = "troubleshooting", "During troubleshooting"
        AFTER_FIX = "after_fix", "After the fix"

    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="attachments")
    comment = models.ForeignKey(TicketComment, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="attachments")
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.REPORTED)
    is_internal = models.BooleanField(default=False, help_text="Only IT staff can see internal evidence.")

    class Meta(EvidenceFile.Meta):
        pass


class TicketStatusHistory(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=15, choices=Ticket.Status.choices, blank=True)
    to_status = models.CharField(max_length=15, choices=Ticket.Status.choices)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    note = models.TextField(blank=True)
    is_reopen = models.BooleanField(default=False)

    class Meta:
        ordering = ["changed_at", "id"]
        verbose_name_plural = "ticket status history"

    def __str__(self):
        return f"{self.ticket.reference}: {self.from_status or '∅'} → {self.to_status}"


class TicketTimeEntry(models.Model):
    """
    Time worked on a ticket. Time recorded on a task linked to this ticket is
    stored here (``via_task``) — never as a TaskTimeEntry — so it counts once.
    """

    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="time_entries")
    via_task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="ticket_time_entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ticket_time_entries")
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    minutes = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=300, blank=True)
    is_manual = models.BooleanField(default=False)
    is_voided = models.BooleanField(default=False, db_index=True)
    void_reason = models.CharField(max_length=300, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        verbose_name_plural = "ticket time entries"
        constraints = [
            models.UniqueConstraint(fields=["user"], condition=models.Q(ended_at__isnull=True, is_voided=False),
                                    name="one_running_ticket_timer_per_user"),
            models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gt=models.F("started_at")),
                                   name="ticket_time_end_after_start"),
        ]

    def __str__(self):
        return f"{self.ticket.reference} {self.minutes} min"

    @property
    def is_running(self):
        return self.ended_at is None and not self.is_voided


class EscalationRule(models.Model):
    """Administrator-defined escalation: when a condition is met, notify the chosen people once per ticket."""

    class Trigger(models.TextChoices):
        UNASSIGNED = "unassigned", "Still unassigned after N minutes"
        RESPONSE_BREACH = "response_breach", "Response target missed"
        RESOLUTION_WARNING = "resolution_warning", "N% of resolution time used"
        RESOLUTION_BREACH = "resolution_breach", "Resolution target missed"

    name = models.CharField(max_length=100)
    trigger = models.CharField(max_length=20, choices=Trigger.choices)
    threshold = models.PositiveIntegerField(default=0, help_text="Minutes for 'unassigned'; percent for 'warning'.")
    priorities = models.CharField(max_length=60, blank=True,
                                  help_text="Comma-separated priorities this applies to; empty means all.")
    notify_assignee = models.BooleanField(default=True)
    notify_it_staff = models.BooleanField(default=False, help_text="All IT support officers.")
    notify_admins = models.BooleanField(default=False)
    notify_department_head = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def priority_list(self):
        return [p.strip() for p in self.priorities.split(",") if p.strip()]


class TicketEscalation(models.Model):
    """Records that a rule fired for a ticket, so each escalation is sent once."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="escalations")
    rule = models.ForeignKey(EscalationRule, on_delete=models.CASCADE, related_name="+")
    fired_at = models.DateTimeField(auto_now_add=True)
    cycle = models.PositiveIntegerField(default=0, help_text="Reopen count when fired; a reopened ticket can escalate again.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ticket", "rule", "cycle"], name="uniq_ticket_escalation")]
