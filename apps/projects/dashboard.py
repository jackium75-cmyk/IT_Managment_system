"""Phase 5 dashboard metric: projects."""
from django.urls import reverse

from apps.accounts.roles import Role
from apps.dashboard.registry import card_provider

from .models import Project

ROLES = (Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)


@card_provider("Projects & approvals", order=5, roles=ROLES)
def project_cards(filters):
    qs = Project.objects.visible_to(filters.user)
    if filters.department_id and filters.extra.get("role") != Role.MANAGER:
        qs = qs.filter(sponsor_department_id=filters.department_id)
    active = qs.active()
    return [
        {"label": "Active IT projects", "value": active.count(), "icon": "kanban", "tone": "primary",
         "url": reverse("projects:list")},
        {"label": "Projects past target date", "value": sum(1 for p in active if p.is_overdue), "icon": "flag",
         "tone": "warning", "url": reverse("projects:list")},
        {"label": "Projects completed in period", "value": qs.filter(
            status=Project.Status.COMPLETED, completed_on__range=(filters.start, filters.end)).count(),
         "icon": "trophy", "tone": "success", "url": reverse("projects:list") + "?status=completed"},
    ]
