from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "user", "source", "activity_type", "title", "is_voided")
    list_filter = ("source", "activity_type", "is_voided")
    search_fields = ("title", "description")
