from django import template
from django.utils.html import format_html

register = template.Library()

STATUS = {"in_use": "text-bg-success", "in_stock": "text-bg-info", "under_repair": "text-bg-warning",
          "retired": "text-bg-secondary", "disposed": "text-bg-dark", "lost": "text-bg-danger"}
CONDITION = {"new": "text-bg-light border", "good": "text-bg-light border", "fair": "text-bg-light border",
             "poor": "text-bg-warning", "broken": "text-bg-danger"}
RECORD = {"scheduled": "text-bg-primary", "completed": "text-bg-success", "missed": "text-bg-danger",
          "cancelled": "text-bg-secondary"}


@register.filter
def asset_status(asset):
    return format_html('<span class="badge {}">{}</span>', STATUS.get(asset.status, ""), asset.get_status_display())


@register.filter
def asset_condition(asset):
    return format_html('<span class="badge {}">{}</span>', CONDITION.get(asset.condition, ""), asset.get_condition_display())


@register.filter
def record_status(record):
    label = record.get_status_display()
    css = RECORD.get(record.status, "")
    if record.is_overdue:
        label, css = "Overdue", "text-bg-danger"
    elif record.status == "completed" and record.on_time is False:
        label, css = "Completed late", "text-bg-warning"
    return format_html('<span class="badge {}">{}</span>', css, label)
