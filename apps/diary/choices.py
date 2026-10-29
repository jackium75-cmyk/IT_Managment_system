from django.db import models


class ActivityType(models.TextChoices):
    """What kind of IT work an activity or time record represents."""

    SUPPORT = "support", "User support / helpdesk"
    MAINTENANCE = "maintenance", "Maintenance"
    SYSTEM_CHECK = "system_check", "System checks / monitoring"
    PROJECT = "project", "Projects & improvements"
    ADMINISTRATION = "administration", "Administration"
    MEETING = "meeting", "Meetings & consultations"
    TRAINING = "training", "Training"
    DOCUMENTATION = "documentation", "Documentation"
    SECURITY = "security", "Security"
    EMERGENCY = "emergency", "Emergency work"
    OTHER = "other", "Other"
