from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import MaintenancePlan, MaintenanceRecord


@admin.register(MaintenancePlan)
class MaintenancePlanAdmin(admin.ModelAdmin):
    list_display = ("title", "asset", "next_due_date", "responsible", "is_active")


@admin.register(MaintenanceRecord)
class MaintenanceRecordAdmin(ReadOnlyAdmin):
    list_display = ("title", "asset", "kind", "status", "due_date", "completed_on")
    list_filter = ("kind", "status")
