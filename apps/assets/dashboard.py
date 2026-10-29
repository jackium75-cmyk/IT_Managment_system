"""Phase 4 dashboard metrics: maintenance due, assets needing attention, repeated failures, compliance."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import IT_STAFF_ROLES
from apps.dashboard.registry import card_provider, chart_provider

from . import services
from .models import Asset

IT = IT_STAFF_ROLES


def _assets(filters):
    qs = Asset.objects.all()
    if filters.department_id:
        qs = qs.filter(department_id=filters.department_id)
    return qs


@card_provider("Assets & maintenance", order=4, roles=IT)
def asset_cards(filters):
    from apps.maintenance.models import MaintenanceRecord

    today = timezone.localdate()
    assets = _assets(filters)
    records = MaintenanceRecord.objects.filter(status="scheduled", asset__in=assets)
    officer = filters.extra.get("officer")
    if officer:
        records = records.filter(responsible_id=officer)
    attention = services.needing_attention(assets)
    return [
        {"label": "Maintenance due in 14 days", "value": records.filter(due_date__range=(today, today + timedelta(days=14))).count(),
         "icon": "calendar-check", "tone": "primary", "url": reverse("maintenance:list") + "?view=upcoming"},
        {"label": "Maintenance overdue", "value": records.filter(due_date__lt=today).count(), "icon": "calendar-x",
         "tone": "danger", "url": reverse("maintenance:list") + "?view=overdue"},
        {"label": "Assets requiring attention", "value": len(attention), "icon": "exclamation-octagon", "tone": "warning",
         "url": reverse("assets:list") + "?attention=1",
         "help": "Under repair, poor condition, replacement recommended, warranty ending, past expected life, "
                 "repeated failures or overdue maintenance."},
        {"label": "Under repair", "value": assets.filter(status=Asset.Status.UNDER_REPAIR).count(), "icon": "wrench",
         "tone": "secondary", "url": reverse("assets:list") + "?status=under_repair"},
        {"label": "Warranties ending (60 days)", "value": assets.in_service().filter(
            warranty_expiry__range=(today, today + timedelta(days=60))).count(),
         "icon": "shield-exclamation", "tone": "secondary", "url": reverse("assets:list") + "?warranty=expiring"},
        {"label": "Assets in service", "value": assets.in_service().count(), "icon": "pc-display", "tone": "secondary",
         "url": reverse("assets:list")},
    ]


@chart_provider(order=5, roles=IT)
def asset_charts(filters):
    from apps.maintenance.services import compliance

    charts = []
    rows = sorted(services.failure_counts(filters.start_dt, filters.end_dt, qs=_assets(filters)),
                  key=lambda a: -(a.incidents + a.repairs))[:10]
    if rows:
        charts.append({
            "id": "chart-failures", "title": "Repeated equipment failures", "type": "bar", "horizontal": True,
            "stacked": True, "labels": [f"{a.tag} {a.name}"[:40] for a in rows],
            "datasets": [{"label": "Incident tickets", "data": [a.incidents for a in rows]},
                         {"label": "Repairs", "data": [a.repairs for a in rows]}],
            "help": "Incident tickets linked to the asset and corrective repairs recorded in the period.",
        })
    c = compliance(filters.start, filters.end)
    if c["due"]:
        charts.append({
            "id": "chart-maint-compliance", "title": "Preventive maintenance compliance", "type": "doughnut",
            "labels": ["On time", "Late", "Missed", "Still open"],
            "datasets": [{"label": "Visits", "data": [c["on_time"], c["late"], c["missed"], c["open"]]}],
            "help": (f"{c['rate']}% on time" if c["rate"] is not None else "No visits decided yet")
                    + f" of {c['due']} visit(s) due in the period. On time = done by the original due date.",
        })
    return charts
