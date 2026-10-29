from django.conf import settings
from django.db import models
from django.urls import reverse

from apps.core.models import TimeStampedModel


class KnowledgeArticle(TimeStampedModel):
    """
    A documented troubleshooting case / solution. Suggested on similar tickets,
    but similar symptoms do not guarantee the same root cause — suggestions are
    always presented as "check whether this applies".
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    title = models.CharField(max_length=200, help_text="Describe the problem as users report it.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    category = models.ForeignKey("helpdesk.TicketCategory", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="articles")
    asset_category = models.ForeignKey("assets.AssetCategory", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="articles", verbose_name="device type")
    keywords = models.CharField(max_length=300, blank=True, help_text="Extra search words, comma-separated.")

    problem = models.TextField("problem reported")
    error_codes = models.CharField("error codes / messages", max_length=500, blank=True,
                                   help_text="One per line or comma-separated, e.g. E-05, 0x80070005.")
    symptoms = models.TextField("symptoms observed", blank=True)
    possible_causes = models.TextField(blank=True)
    diagnostics = models.TextField("diagnostic steps performed", blank=True)
    commands = models.TextField("commands / configuration changes", blank=True,
                                help_text="Never include passwords or keys.")
    parts_replaced = models.TextField(blank=True)
    root_cause = models.TextField("root cause (where known)", blank=True)
    solution = models.TextField("solution applied")
    verification = models.TextField("verification tests", blank=True)
    prevention = models.TextField("preventive recommendations", blank=True)

    is_verified = models.BooleanField(default=False, db_index=True,
                                      help_text="Verified = the solution has been confirmed to fix this problem.")
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)

    source_ticket = models.ForeignKey("helpdesk.Ticket", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="source_articles")
    related_assets = models.ManyToManyField("assets.Asset", blank=True, related_name="articles")
    used_in_tickets = models.ManyToManyField("helpdesk.Ticket", blank=True, related_name="applied_articles",
                                             help_text="Tickets where this solution was applied.")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="articles")

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("knowledge:detail", args=[self.pk])

    @property
    def error_code_list(self):
        import re

        return [c.strip() for c in re.split(r"[,\n]", self.error_codes) if c.strip()]
