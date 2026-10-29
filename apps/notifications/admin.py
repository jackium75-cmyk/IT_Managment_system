from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "recipient", "kind", "title", "is_read", "emailed_at")
    list_filter = ("kind", "is_read")
