from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

from apps.core.models import TimeStampedModel
from apps.evidence.models import EvidenceFile


class ProcurementQuerySet(models.QuerySet):
    def visible_to(self, user):
        from apps.accounts.roles import Role, get_role

        role = get_role(user)
        if role in (Role.ADMIN, Role.IT_OFFICER):
            return self
        if role == Role.MANAGER and user.profile.department_id:
            return self.filter(department_id=user.profile.department_id)
        return self.none()


class ProcurementRequest(TimeStampedModel):
    """
    Tracks IT purchases, repairs and services from request to delivery.
    This is an expenditure log for IT decisions — not an accounting system.
    """

    class Type(models.TextChoices):
        PURCHASE = "purchase", "Equipment purchase"
        REPAIR = "repair", "External repair"
        SOFTWARE = "software", "Software / licence"
        SERVICE = "service", "Service / subscription"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        ORDERED = "ordered", "Ordered"
        DELIVERED = "delivered", "Delivered / done"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.DRAFT, Status.PENDING, Status.APPROVED, Status.ORDERED)

    reference = models.CharField(max_length=20, unique=True, editable=False)
    request_type = models.CharField("type", max_length=10, choices=Type.choices, default=Type.PURCHASE)
    item = models.CharField("item or service", max_length=200)
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    reason = models.TextField(help_text="Why it is needed and what happens if it is not bought.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)

    department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="procurement_requests", help_text="Department it is for.")
    asset = models.ForeignKey("assets.Asset", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="procurement_requests")
    ticket = models.ForeignKey("helpdesk.Ticket", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="procurement_requests")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="procurement_requests")
    supplier = models.ForeignKey("assets.Supplier", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="procurement_requests")

    currency = models.CharField(max_length=3)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    approved_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                        validators=[MinValueValidator(0)])
    actual_expenditure = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                             validators=[MinValueValidator(0)])
    purchase_date = models.DateField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    warranty = models.CharField(max_length=200, blank=True)
    outcome = models.TextField(blank=True, help_text="What was received / done, and the result.")
    cost_saving_note = models.TextField(blank=True, help_text="Only documented savings, e.g. 'repair instead of "
                                                              "replacement: quotation for new unit attached'.")

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="procurement_requests")
    submitted_at = models.DateTimeField(null=True, blank=True)
    requires_approval = models.BooleanField(default=True, editable=False)

    objects = ProcurementQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} {self.item}"

    def get_absolute_url(self):
        return reverse("procurement:detail", args=[self.pk])

    def clean(self):
        if self.delivery_date and self.purchase_date and self.delivery_date < self.purchase_date:
            raise ValidationError({"delivery_date": "Delivery cannot be before the purchase date."})

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def variance(self):
        """Actual minus approved (or estimated) cost, when both are known."""
        base = self.approved_cost if self.approved_cost is not None else self.estimated_cost
        if self.actual_expenditure is None or base is None:
            return None
        return self.actual_expenditure - base


class Approval(models.Model):
    """One approval decision. A request can be resubmitted, so it may have several."""

    class Decision(models.TextChoices):
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    request = models.ForeignKey(ProcurementRequest, on_delete=models.CASCADE, related_name="approvals")
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approvals")
    decision = models.CharField(max_length=10, choices=Decision.choices)
    approved_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    comment = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-decided_at", "-id"]

    def __str__(self):
        return f"{self.request.reference} {self.decision} by {self.approver}"


class ProcurementAttachment(EvidenceFile):
    class DocType(models.TextChoices):
        QUOTATION = "quotation", "Quotation"
        INVOICE = "invoice", "Invoice / receipt"
        DELIVERY = "delivery", "Delivery note"
        OTHER = "other", "Other"

    request = models.ForeignKey(ProcurementRequest, on_delete=models.PROTECT, related_name="attachments")
    doc_type = models.CharField(max_length=10, choices=DocType.choices, default=DocType.QUOTATION)

    class Meta(EvidenceFile.Meta):
        pass
