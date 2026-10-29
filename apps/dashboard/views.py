from datetime import datetime, timedelta

from django.contrib import messages
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import Department
from apps.accounts.roles import Role, get_role
from apps.core.permissions import role_required

from .registry import DashboardFilters, build_cards, build_charts

DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 366

# Every module now contributes its own cards; nothing is pending.
UPCOMING = []


def _parse(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@role_required(Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)
def index(request):
    role = get_role(request.user)
    today = timezone.localdate()
    end = _parse(request.GET.get("end")) or today
    start = _parse(request.GET.get("start")) or end - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    if start > end:
        start, end = end, start
        messages.info(request, "The start date was after the end date, so they were swapped.")
    if (end - start).days >= MAX_PERIOD_DAYS:
        start = end - timedelta(days=MAX_PERIOD_DAYS - 1)
        messages.info(request, "The dashboard shows at most one year; use Reports for longer periods.")

    department_id = request.GET.get("department")
    department_id = int(department_id) if department_id and department_id.isdigit() else None
    departments = Department.objects.active()
    if role == Role.MANAGER:
        # Managers only ever see their own department, whatever the URL says.
        department_id = request.user.profile.department_id
        departments = departments.filter(pk=department_id)

    extra = {"role": role}
    if role in (Role.ADMIN, Role.IT_OFFICER):
        from apps.tasks.forms import it_staff_queryset
        from apps.tasks.models import Priority, Task, TaskCategory

        category = request.GET.get("category", "")
        officer = request.GET.get("officer", "")
        extra.update({
            "category": int(category) if category.isdigit() else None,
            "priority": request.GET.get("priority") if request.GET.get("priority") in Priority.values else None,
            "status": request.GET.get("status") if request.GET.get("status") in Task.Status.values else None,
            "officer": int(officer) if officer.isdigit() else None,
        })
        options = {"categories": TaskCategory.objects.all(), "priorities": Priority.choices,
                   "statuses": Task.Status.choices, "officers": it_staff_queryset()}
    else:
        options = {}
    filters = DashboardFilters(user=request.user, start=start, end=end, department_id=department_id, extra=extra)
    return render(request, "dashboard/index.html", {
        "filters": filters,
        "departments": departments,
        "card_sections": build_cards(filters, role),
        "charts": build_charts(filters, role),
        "upcoming": UPCOMING if role in (Role.ADMIN, Role.IT_OFFICER) else [],
        "locked_department": role == Role.MANAGER,
        "options": options,
    })
