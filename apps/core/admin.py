from django.contrib import admin

from .models import AuditLog, SystemSetting


class ReadOnlyAdmin(admin.ModelAdmin):
    """Business records can only be viewed in Django's admin site.

    Changes must go through the application, whose services enforce the rules (reasons, history, approvals)
    and write the audit log. The admin site is kept for emergency inspection by superusers.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "actor_username", "action", "object_repr", "ip_address")
    list_filter = ("action",)
    search_fields = ("actor_username", "object_repr", "message")


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not SystemSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
