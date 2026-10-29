import zoneinfo

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeStampedModel(models.Model):
    """Adds created/updated timestamps to every business record."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def validate_timezone(value):
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"{value!r} is not a recognised IANA timezone (e.g. Africa/Nairobi).")


DEFAULT_ALLOWED_EXTENSIONS = (
    "jpg,jpeg,png,gif,webp,heic,mp4,m4v,mov,webm,3gp,pdf,txt,log,csv,docx,xlsx"
)


class SystemSetting(models.Model):
    """
    Singleton holding hotel-wide configuration that administrators can change
    at runtime. Always access it through ``SystemSetting.get()``.
    """

    CACHE_KEY = "core:system_setting"

    hotel_name = models.CharField(max_length=150, default="Hotel")
    hotel_timezone = models.CharField(
        max_length=64, default="UTC", validators=[validate_timezone],
        help_text="IANA timezone used to display all dates and times, e.g. Africa/Nairobi.",
    )
    it_department_email = models.EmailField(blank=True)
    it_department_phone = models.CharField(max_length=40, blank=True)

    # Uploads (enforced from Phase 3 onwards)
    max_upload_size_mb = models.PositiveIntegerField(
        default=50, validators=[MinValueValidator(1), MaxValueValidator(1024)],
        help_text="Largest single evidence file accepted, in megabytes.",
    )
    allowed_upload_extensions = models.CharField(
        max_length=500, default=DEFAULT_ALLOWED_EXTENSIONS,
        help_text="Comma-separated list of permitted file extensions.",
    )

    # Security
    login_max_failures = models.PositiveIntegerField(
        default=5, validators=[MinValueValidator(3), MaxValueValidator(20)],
        help_text="Failed sign-ins allowed before an account/IP pair is locked.",
    )
    login_lockout_minutes = models.PositiveIntegerField(
        default=15, validators=[MinValueValidator(1), MaxValueValidator(1440)],
    )

    # Notifications
    email_notifications_enabled = models.BooleanField(
        default=False, help_text="Requires the email server settings in .env.")

    currency = models.CharField(max_length=3, default="USD",
                                help_text="Default currency code for costs, e.g. KES, USD, EUR.")

    # Procurement
    procurement_requires_approval = models.BooleanField(
        default=True, help_text="Purchase and repair requests need approval by the department head or an administrator.")
    procurement_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)],
        help_text="Requests estimated below this amount are approved automatically (0 = everything needs approval).",
    )

    # Maintenance
    maintenance_grace_days = models.PositiveIntegerField(
        default=7, validators=[MinValueValidator(0), MaxValueValidator(90)],
        help_text="A scheduled maintenance visit counts as missed this many days after its due date.",
    )

    # Helpdesk
    ticket_auto_close_days = models.PositiveIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(60)],
        help_text="Resolved tickets close automatically if the requester does not respond within this many days.",
    )
    ticket_reopen_days = models.PositiveIntegerField(
        default=14, validators=[MinValueValidator(1), MaxValueValidator(365)],
        help_text="Requesters can reopen a closed ticket for this many days after it was closed.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "system setting"
        verbose_name_plural = "system settings"

    def __str__(self):
        return f"System settings for {self.hotel_name}"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    def delete(self, *args, **kwargs):
        raise ValidationError("System settings cannot be deleted.")

    @property
    def allowed_extensions_list(self):
        return sorted({e.strip().lower().lstrip(".") for e in self.allowed_upload_extensions.split(",") if e.strip()})

    @classmethod
    def get(cls):
        obj = cache.get(cls.CACHE_KEY)
        if obj is None:
            obj, _ = cls.objects.get_or_create(
                pk=1,
                defaults={
                    "hotel_name": getattr(settings, "HOTEL_NAME_DEFAULT", "Hotel"),
                    "hotel_timezone": settings.TIME_ZONE,
                    "login_max_failures": settings.LOGIN_MAX_FAILURES,
                    "login_lockout_minutes": settings.LOGIN_LOCKOUT_MINUTES,
                },
            )
            cache.set(cls.CACHE_KEY, obj, 300)
        return obj


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit log entries are immutable.")

    def delete(self):
        raise ValidationError("Audit log entries cannot be deleted.")


class AuditLog(models.Model):
    """
    Append-only record of security-relevant and business-relevant events.
    Entries can be created but never updated or deleted through the ORM.
    """

    class Action(models.TextChoices):
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        LOGIN_FAILED = "login_failed", "Login failed"
        LOGIN_LOCKED = "login_locked", "Login locked out"
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        STATUS_CHANGE = "status_change", "Status change"
        ASSIGN = "assign", "Assignment change"
        DEACTIVATE = "deactivate", "Deactivate"
        ACTIVATE = "activate", "Activate"
        DELETE = "delete", "Delete / archive"
        PASSWORD_CHANGE = "password_change", "Password change"
        PASSWORD_RESET = "password_reset", "Password reset by admin"
        CONFIG_CHANGE = "config_change", "Configuration change"
        UPLOAD = "upload", "Attachment upload"
        APPROVAL = "approval", "Approval decision"
        REPORT = "report", "Report generated"
        ACCESS_DENIED = "access_denied", "Access denied"
        BACKUP = "backup", "Backup / restore"
        OTHER = "other", "Other"

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries"
    )
    actor_username = models.CharField(
        max_length=150, blank=True, help_text="Username at the time of the event (kept even if the user is renamed)."
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    content_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.SET_NULL)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)
    object_repr = models.CharField(max_length=255, blank=True)
    message = models.CharField(max_length=500, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.actor_username or 'system'} {self.action} {self.object_repr}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Audit log entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit log entries cannot be deleted.")


class Sequence(models.Model):
    """
    Gap-tolerant, concurrency-safe counters for human-readable references
    (TSK-2026-00001, later INC-/REQ- for tickets). Use ``Sequence.next()``.
    """

    name = models.CharField(max_length=30)
    year = models.PositiveIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "year"], name="uniq_sequence_name_year")]

    def __str__(self):
        return f"{self.name}-{self.year}: {self.last_value}"

    @classmethod
    def next(cls, name, year):
        """Return the next value. Must be called inside ``transaction.atomic()``."""
        from django.db import transaction

        if not transaction.get_connection().in_atomic_block:
            raise RuntimeError("Sequence.next() must run inside a transaction.")
        cls.objects.get_or_create(name=name, year=year)
        seq = cls.objects.select_for_update(of=("self",)).get(name=name, year=year)
        seq.last_value += 1
        seq.save(update_fields=["last_value"])
        return seq.last_value
