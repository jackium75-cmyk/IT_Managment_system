import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.evidence.models import EvidenceFile

SECRET_PATTERN = re.compile(
    r"\b(pass(word|wd|phrase)?|pwd|pin|psk|wpa2?|secret|api[_ -]?key|token|licen[cs]e[_ -]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def validate_no_secrets(value):
    """Asset records must never hold credentials (they can be printed, exported and scanned)."""
    if value and SECRET_PATTERN.search(value):
        raise ValidationError(
            "This looks like a password or key. Do not store credentials in the asset register; "
            "keep them in the hotel's password manager."
        )


class AssetCategory(TimeStampedModel):
    name = models.CharField(max_length=80)
    code = models.CharField(
        max_length=6, unique=True, validators=[RegexValidator(r"^[A-Z]{2,6}$", "2–6 capital letters, e.g. PRN.")],
        help_text="Prefix for asset tags, e.g. PRN → PRN-0001.",
    )
    default_maintenance_days = models.PositiveIntegerField(
        null=True, blank=True, help_text="Suggested preventive-maintenance interval for new plans (days).")
    expected_life_years = models.PositiveIntegerField(null=True, blank=True,
                                                      help_text="Used to flag equipment due for replacement.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "asset categories"
        constraints = [models.UniqueConstraint(Lower("name"), name="uniq_asset_category_name_ci")]

    def __str__(self):
        return self.name


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True, validators=[validate_no_secrets])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="uniq_supplier_name_ci")]

    def __str__(self):
        return self.name


class AssetQuerySet(models.QuerySet):
    def in_service(self):
        return self.exclude(status__in=(Asset.Status.RETIRED, Asset.Status.DISPOSED, Asset.Status.LOST))


class Asset(TimeStampedModel):
    class Status(models.TextChoices):
        IN_USE = "in_use", "In use"
        IN_STOCK = "in_stock", "In stock / spare"
        UNDER_REPAIR = "under_repair", "Under repair"
        RETIRED = "retired", "Retired"
        DISPOSED = "disposed", "Disposed"
        LOST = "lost", "Lost / stolen"

    class Condition(models.TextChoices):
        NEW = "new", "New"
        GOOD = "good", "Good"
        FAIR = "fair", "Fair"
        POOR = "poor", "Poor"
        BROKEN = "broken", "Not working"

    class Replacement(models.TextChoices):
        NONE = "none", "No replacement needed"
        CONSIDER = "consider", "Consider replacing"
        RECOMMENDED = "recommended", "Replacement recommended"
        URGENT = "urgent", "Replace urgently"

    tag = models.CharField("asset ID", max_length=20, unique=True, editable=False)
    name = models.CharField(max_length=150, help_text="e.g. 'Reception PC 1', 'Core switch'.")
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name="assets")
    manufacturer = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)

    department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="assets")
    location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="assets")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="assets")

    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                        validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, blank=True)
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT, related_name="assets")
    warranty_expiry = models.DateField(null=True, blank=True)
    warranty_details = models.TextField(blank=True, validators=[validate_no_secrets])

    condition = models.CharField(max_length=10, choices=Condition.choices, default=Condition.GOOD)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.IN_USE, db_index=True)

    ip_address = models.GenericIPAddressField("IP address", null=True, blank=True)
    mac_address = models.CharField(
        "MAC address", max_length=17, blank=True,
        validators=[RegexValidator(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", "Use the form AA:BB:CC:DD:EE:FF.")])
    hostname = models.CharField(max_length=100, blank=True)
    operating_system = models.CharField(max_length=80, blank=True)
    os_version = models.CharField("OS version", max_length=60, blank=True)
    software = models.TextField("software and licences", blank=True, validators=[validate_no_secrets],
                                help_text="Installed software, licence types and expiry dates. Never licence keys or passwords.")

    replacement = models.CharField("replacement recommendation", max_length=12, choices=Replacement.choices,
                                   default=Replacement.NONE)
    replacement_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True, validators=[validate_no_secrets])

    qr_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
                                help_text="Random identifier in the QR code; it reveals nothing about the asset.")

    objects = AssetQuerySet.as_manager()

    class Meta:
        ordering = ["tag"]
        indexes = [models.Index(fields=["status", "category"])]
        constraints = [
            models.UniqueConstraint(Lower("serial_number"), "manufacturer", name="uniq_asset_serial",
                                    condition=~models.Q(serial_number="")),
        ]

    def __str__(self):
        return f"{self.tag} {self.name}"

    def get_absolute_url(self):
        return reverse("assets:detail", args=[self.pk])

    @property
    def in_service(self):
        return self.status not in (self.Status.RETIRED, self.Status.DISPOSED, self.Status.LOST)

    @property
    def warranty_status(self):
        if not self.warranty_expiry:
            return "unknown"
        today = timezone.localdate()
        if self.warranty_expiry < today:
            return "expired"
        if self.warranty_expiry <= today + timedelta(days=60):
            return "expiring"
        return "valid"

    @property
    def age_years(self):
        if not self.purchase_date:
            return None
        return round((timezone.localdate() - self.purchase_date).days / 365.25, 1)

    @property
    def beyond_expected_life(self):
        life = self.category.expected_life_years
        return bool(life and self.age_years is not None and self.age_years >= life)

    def clean(self):
        if self.purchase_cost is not None and not self.currency:
            raise ValidationError({"currency": "Give the currency of the purchase cost."})


class AssetAssignmentHistory(models.Model):
    """Who had the asset and where it was, over time."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="assignment_history")
    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    from_department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    to_department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    from_location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    to_location = models.ForeignKey("accounts.Location", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        verbose_name_plural = "asset assignment history"

    def __str__(self):
        return f"{self.asset.tag} moved {self.changed_at:%Y-%m-%d}"


class AssetAttachment(EvidenceFile):
    """Invoices, warranty certificates, manuals, photos of the equipment."""

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="attachments")

    class Meta(EvidenceFile.Meta):
        pass
