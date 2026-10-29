from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import BackupRecord


@admin.register(BackupRecord)
class BackupRecordAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "kind", "status", "file_name", "size_bytes", "verify_status")
    list_filter = ("status", "kind", "verify_status")
