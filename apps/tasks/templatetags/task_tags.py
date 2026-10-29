from django import template
from django.utils.html import format_html

from apps.diary.timecalc import format_minutes

register = template.Library()

STATUS_CLASS = {
    "planned": "text-bg-light border", "not_started": "text-bg-secondary", "in_progress": "text-bg-primary",
    "waiting_info": "text-bg-warning", "waiting_parts": "text-bg-warning", "blocked": "text-bg-danger",
    "completed": "text-bg-success", "cancelled": "text-bg-dark",
}
PRIORITY_CLASS = {"low": "text-bg-light border", "medium": "text-bg-info", "high": "text-bg-warning",
                  "critical": "text-bg-danger"}


@register.filter
def status_badge(task):
    return format_html('<span class="badge {}">{}</span>', STATUS_CLASS.get(task.status, "text-bg-light"),
                       task.get_status_display())


@register.filter
def priority_badge(obj):
    icon = '<i class="bi bi-exclamation-octagon-fill me-1" aria-hidden="true"></i>' if obj.priority == "critical" else ""
    return format_html('<span class="badge {}">' + icon + '{}</span>', PRIORITY_CLASS.get(obj.priority, ""),
                       obj.get_priority_display())


@register.filter
def minutes(value):
    return format_minutes(value)
