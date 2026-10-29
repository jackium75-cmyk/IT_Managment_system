import hashlib
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def evidence_path(instance, filename):
    """Random storage name: the user's file name is never used on disk."""
    from .validation import extension_of

    now = timezone.now()
    return f"evidence/{now:%Y/%m}/{uuid.uuid4().hex}.{extension_of(filename)}"


class EvidenceFile(models.Model):
    """
    Abstract base for attachments. The binary lives on disk (outside the web
    root); the database keeps metadata and a SHA-256 fingerprint for integrity.
    Attachments are hidden (soft-deleted), never removed, so history stays intact.
    """

    class Kind(models.TextChoices):
        IMAGE = "image", "Photo / image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"

    file = models.FileField(upload_to=evidence_path, max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64, editable=False)
    description = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_deleted = models.BooleanField(default=False)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    deleted_at = models.DateTimeField(null=True, blank=True)
    delete_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        abstract = True
        ordering = ["uploaded_at", "id"]

    def __str__(self):
        return self.original_name

    @property
    def size_display(self):
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024

    @property
    def is_image(self):
        return self.kind == self.Kind.IMAGE and self.content_type != "image/heic"

    @property
    def is_video(self):
        return self.kind == self.Kind.VIDEO

    @staticmethod
    def fingerprint(uploaded):
        digest = hashlib.sha256()
        for chunk in uploaded.chunks():
            digest.update(chunk)
        uploaded.seek(0)
        return digest.hexdigest()
