"""Phase 7 dashboard metric: backup health (administrators only)."""
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from apps.accounts.roles import Role
from apps.dashboard.registry import card_provider

from . import services


@card_provider("Operations", order=9, roles=(Role.ADMIN,))
def backup_cards(filters):
    h = services.health()
    last = h["last"]
    return [{
        "label": "Last successful backup",
        "value": f"{timesince(last.started_at, timezone.now())} ago" if last else "Never",
        "icon": "shield-check" if not h["overdue"] else "exclamation-octagon",
        "tone": "danger" if h["overdue"] else "success",
        "url": reverse("backups:list"),
        "help": "More than a day old: check the scheduled backup task." if h["overdue"] else
                ("Verified." if last and last.verify_status == "ok" else "Not yet verified."),
    }]
