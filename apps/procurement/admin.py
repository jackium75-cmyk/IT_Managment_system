from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import Approval, ProcurementRequest


@admin.register(ProcurementRequest)
class ProcurementRequestAdmin(ReadOnlyAdmin):
    list_display = ("reference", "item", "status", "estimated_cost", "currency", "requested_by")
    readonly_fields = ("reference",)


@admin.register(Approval)
class ApprovalAdmin(ReadOnlyAdmin):
    list_display = ("request", "approver", "decision", "decided_at")
