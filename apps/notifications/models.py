from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Kind(models.TextChoices):
        TICKET_NEW = "ticket_new", "New ticket"
        TICKET_ASSIGNED = "ticket_assigned", "Ticket assigned"
        TICKET_STATUS = "ticket_status", "Ticket status changed"
        TICKET_INFO = "ticket_info", "Information requested"
        TICKET_REPLY = "ticket_reply", "New reply"
        TICKET_RESOLVED = "ticket_resolved", "Ticket resolved"
        TICKET_REOPENED = "ticket_reopened", "Ticket reopened"
        CRITICAL = "critical", "Critical incident"
        ESCALATION = "escalation", "Escalation"
        DEADLINE = "deadline", "Upcoming deadline"
        OVERDUE = "overdue", "Overdue work"
        MAINTENANCE = "maintenance", "Maintenance due"
        APPROVAL = "approval", "Approval pending"
        SYSTEM = "system", "System alert"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    email_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "is_read", "created_at"])]

    def __str__(self):
        return f"{self.recipient} · {self.title}"
