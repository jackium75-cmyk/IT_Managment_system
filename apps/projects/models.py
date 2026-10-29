from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.evidence.models import EvidenceFile
from apps.tasks.models import Priority


class ProjectQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status__in=(Project.Status.APPROVED, Project.Status.IN_PROGRESS, Project.Status.ON_HOLD))

    def visible_to(self, user):
        from apps.accounts.roles import Role, get_role

        role = get_role(user)
        if role in (Role.ADMIN, Role.IT_OFFICER):
            return self
        if role == Role.MANAGER and user.profile.department_id:
            return self.filter(sponsor_department_id=user.profile.department_id)
        return self.none()


class Project(TimeStampedModel):
    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        APPROVED = "approved", "Approved"
        IN_PROGRESS = "in_progress", "In progress"
        ON_HOLD = "on_hold", "On hold"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    code = models.CharField(max_length=20, unique=True, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField()
    business_objective = models.TextField(help_text="Why the hotel needs this: the problem solved or the benefit sought.")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROPOSED, db_index=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_projects")
    sponsor_department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="projects", help_text="Department that benefits / sponsors it.")
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField("target completion date", null=True, blank=True)
    completed_on = models.DateField(null=True, blank=True)

    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                         validators=[MinValueValidator(0)])
    other_costs = models.DecimalField("other recorded costs", max_digits=12, decimal_places=2, null=True, blank=True,
                                      validators=[MinValueValidator(0)],
                                      help_text="Costs not tracked as procurement requests (e.g. paid by another budget).")
    currency = models.CharField(max_length=3, blank=True)

    risks = models.TextField(blank=True)
    blockers = models.TextField(blank=True)
    deliverables = models.TextField("completed deliverables", blank=True)
    final_outcome = models.TextField(blank=True)
    benefits = models.TextField("benefits to the hotel", blank=True,
                                help_text="Describe benefits observed. Say how you know (measurement, feedback).")
    documented_savings = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)],
        help_text="Only enter savings you can evidence. Leave empty if none are documented.")
    savings_evidence = models.TextField(blank=True, help_text="Required when savings are entered: how they were calculated "
                                                               "and where the evidence is (invoice comparison, contract, etc.).")
    benefits_verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                             related_name="+", help_text="Manager who confirmed the benefits, if any.")
    benefits_verified_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} {self.name}"

    def get_absolute_url(self):
        return reverse("projects:detail", args=[self.pk])

    def clean(self):
        errors = {}
        if self.start_date and self.target_date and self.target_date < self.start_date:
            errors["target_date"] = "The target date must be after the start date."
        if self.documented_savings and not self.savings_evidence.strip():
            errors["savings_evidence"] = "Explain how the savings are evidenced, or leave the savings empty."
        if (self.estimated_cost is not None or self.other_costs is not None) and not self.currency:
            errors["currency"] = "Give the currency of the costs."
        if errors:
            raise ValidationError(errors)

    @property
    def is_overdue(self):
        return self.status in (self.Status.APPROVED, self.Status.IN_PROGRESS, self.Status.ON_HOLD) and \
            self.target_date is not None and self.target_date < timezone.localdate()

    @property
    def progress(self):
        """Percent of milestones done (None when there are no milestones)."""
        total = self.milestones.count()
        if not total:
            return None
        return round(100 * self.milestones.filter(completed_on__isnull=False).count() / total)


class ProjectMilestone(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    title = models.CharField(max_length=200)
    due_date = models.DateField(null=True, blank=True)
    completed_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "due_date", "id"]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        return self.completed_on is None and self.due_date is not None and self.due_date < timezone.localdate()


class ProjectUpdate(models.Model):
    """Progress log: what happened, decisions, risks raised. Kept in order; never edited."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="updates")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    status_snapshot = models.CharField(max_length=12, choices=Project.Status.choices, blank=True)
    progress_snapshot = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.project.code} update {self.created_at:%Y-%m-%d}"


class ProjectAttachment(EvidenceFile):
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="attachments")

    class Meta(EvidenceFile.Meta):
        pass
