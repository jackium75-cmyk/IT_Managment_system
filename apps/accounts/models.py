from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel

from .roles import Role


class ActiveQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)


class Department(TimeStampedModel):
    """A hotel department (Front Office, F&B, Housekeeping, ...). Deactivated, never deleted."""

    name = models.CharField(max_length=120)
    code = models.CharField(
        max_length=20,
        validators=[RegexValidator(r"^[A-Z0-9_-]+$", "Use upper-case letters, digits, '-' or '_'.")],
        help_text="Short code used in references and reports, e.g. FO, FNB, HK.",
    )
    description = models.TextField(blank=True)
    head = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="headed_departments", help_text="Department head / manager.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    objects = ActiveQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="uniq_department_name_ci"),
            models.UniqueConstraint(fields=["code"], name="uniq_department_code"),
        ]

    def __str__(self):
        return self.name


class Location(TimeStampedModel):
    """A physical place in the hotel: room, office, floor, outlet, server room."""

    name = models.CharField(max_length=120)
    building = models.CharField(max_length=80, blank=True)
    floor = models.CharField(max_length=30, blank=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="locations"
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = ActiveQuerySet.as_manager()

    class Meta:
        ordering = ["building", "floor", "name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), Lower("building"), Lower("floor"), name="uniq_location_ci"),
        ]

    def __str__(self):
        parts = [p for p in (self.building, self.floor) if p]
        return f"{self.name} ({', '.join(parts)})" if parts else self.name


class UserProfile(TimeStampedModel):
    """Hotel-specific information and the role of each user account."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.EMPLOYEE, db_index=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="members"
    )
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Usual work location (pre-fills support requests).",
    )
    job_title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    extension = models.CharField("phone extension", max_length=10, blank=True)
    email_notifications = models.BooleanField(
        default=True, help_text="Also send notifications by email (when email is enabled for the hotel)."
    )
    must_change_password = models.BooleanField(
        default=False, help_text="Force the user to choose a new password at next sign-in."
    )

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.get_role_display()})"


class LoginAttempt(models.Model):
    """Every sign-in attempt, used for brute-force protection."""

    username = models.CharField(max_length=150, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    successful = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["username", "timestamp"]), models.Index(fields=["ip_address", "timestamp"])]

    def __str__(self):
        return f"{self.username} {'ok' if self.successful else 'FAILED'} {self.timestamp:%Y-%m-%d %H:%M}"
