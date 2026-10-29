"""Phase 5 dashboard metrics: approvals and recorded IT expenditure."""
from django.urls import reverse

from apps.accounts.roles import Role
from apps.dashboard.registry import card_provider

from . import services
from .models import ProcurementRequest

ROLES = (Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)


@card_provider("Projects & approvals", order=6, roles=ROLES)
def procurement_cards(filters):
    visible = ProcurementRequest.objects.visible_to(filters.user)
    if filters.department_id:
        visible = visible.filter(department_id=filters.department_id)
    spend = services.spend_by_currency(visible.filter(delivery_date__range=(filters.start, filters.end)))
    return [
        {"label": "Waiting for your approval", "value": services.pending_for(filters.user).count(), "icon": "patch-question",
         "tone": "danger", "url": reverse("procurement:list") + "?view=approval"},
        {"label": "Purchase / repair requests open", "value": visible.filter(status__in=ProcurementRequest.OPEN_STATUSES).count(),
         "icon": "cart3", "tone": "secondary", "url": reverse("procurement:list")},
        {"label": "IT expenditure recorded in period",
         "value": "; ".join(f"{v:,.0f} {k}" for k, v in sorted(spend.items())) or "None",
         "icon": "cash-coin", "tone": "secondary", "url": reverse("procurement:list") + "?view=delivered",
         "help": "Actual amounts on delivered requests, per currency. Not an accounting figure."},
    ]
