from django import template
from django.utils import timezone
from django.utils.html import format_html

register = template.Library()

STATUS_CLASS = {
    "new": "text-bg-primary", "acknowledged": "text-bg-info", "in_progress": "text-bg-primary",
    "waiting": "text-bg-warning", "resolved": "text-bg-success", "closed": "text-bg-secondary",
}


@register.filter
def ticket_status(ticket):
    label = ticket.get_status_display()
    if ticket.status == "waiting" and ticket.waiting_on:
        label += f" – {ticket.get_waiting_on_display().split(' (')[0].lower()}"
    return format_html('<span class="badge {}">{}</span>', STATUS_CLASS.get(ticket.status, "text-bg-light"), label)


@register.filter
def due_badge(ticket):
    """Time left / overdue against the resolution target."""
    if not ticket.is_open or not ticket.resolution_due_at:
        return ""
    if ticket.status == "waiting":
        return format_html('<span class="badge text-bg-light border" title="Target paused while waiting">{}</span>', "paused")
    due = ticket.effective_resolution_due
    delta = due - timezone.now()
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        return format_html('<span class="badge text-bg-danger">overdue {}</span>', _span(-minutes))
    css = "text-bg-warning" if minutes < 60 else "text-bg-light border"
    return format_html('<span class="badge {}">due in {}</span>', css, _span(minutes))


def _span(minutes):
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)} d"
    if minutes >= 60:
        return f"{minutes // 60} h"
    return f"{minutes} min"
