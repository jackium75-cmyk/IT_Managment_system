from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import EscalationRule, SLATarget, Ticket, TicketCategory


@admin.register(Ticket)
class TicketAdmin(ReadOnlyAdmin):
    list_display = ("reference", "subject", "status", "priority", "requester", "assigned_to", "created_at")
    list_filter = ("status", "priority", "ticket_type")
    search_fields = ("reference", "subject")
    readonly_fields = ("reference",)


admin.site.register(TicketCategory)
admin.site.register(SLATarget)
admin.site.register(EscalationRule)
