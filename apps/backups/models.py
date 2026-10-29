from django.conf import settings
from django.db import models


class BackupRecord(models.Model):
    """History of backups made by this installation (the files live in BACKUP_ROOT)."""

    class Kind(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Manual"
        PRE_RESTORE = "pre_restore", "Safety copy before restore"
        FOUND = "found", "Found in backup folder"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Succeeded"
        FAILED = "failed", "Failed"
        ROTATED = "rotated", "Removed"

    class Verify(models.TextChoices):
        NOT_VERIFIED = "not_verified", "Not verified"
        RUNNING = "running", "Verifying"
        OK = "ok", "Verified"
        FAILED = "failed", "Verification failed"

    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.MANUAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING, db_index=True)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    file_name = models.CharField(max_length=200, blank=True, db_index=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    db_engine = models.CharField(max_length=20, blank=True)
    media_files = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    copied_to = models.CharField(max_length=500, blank=True, help_text="Second location the file was copied to.")
    copy_error = models.TextField(blank=True)
    verify_status = models.CharField(max_length=14, choices=Verify.choices, default=Verify.NOT_VERIFIED)
    verified_at = models.DateTimeField(null=True, blank=True)
    verify_message = models.TextField(blank=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return self.file_name or f"Backup {self.started_at:%Y-%m-%d %H:%M}"
