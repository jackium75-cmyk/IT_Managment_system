from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import RecurringTask, Task, TaskCategory


@admin.register(TaskCategory)
class TaskCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "activity_type", "is_active")


@admin.register(Task)
class TaskAdmin(ReadOnlyAdmin):
    list_display = ("number", "title", "status", "planned_date", "assigned_to")
    list_filter = ("status", "priority", "category")
    search_fields = ("number", "title")
    readonly_fields = ("number", "original_planned_date")


@admin.register(RecurringTask)
class RecurringTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "frequency", "assigned_to", "is_active")

    def has_delete_permission(self, request, obj=None):
        return False
