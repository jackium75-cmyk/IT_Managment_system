from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import Project


@admin.register(Project)
class ProjectAdmin(ReadOnlyAdmin):
    list_display = ("code", "name", "status", "owner", "target_date")
    readonly_fields = ("code",)
