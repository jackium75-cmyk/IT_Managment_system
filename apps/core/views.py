from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role, get_role

from . import audit
from .forms import SystemSettingForm
from .models import AuditLog, SystemSetting
from .permissions import role_required

SETTING_FIELDS = SystemSettingForm.Meta.fields


@login_required
def home(request):
    """Send each user to the right landing page for their role."""
    if get_role(request.user) == Role.EMPLOYEE:
        return redirect("core:employee_home")
    return redirect("dashboard:index")


@login_required
def employee_home(request):
    from django.db.models import Q

    from apps.helpdesk.models import Ticket

    mine = Ticket.objects.filter(requester=request.user)
    needs_action = mine.filter(Q(status=Ticket.Status.RESOLVED)
                               | Q(status=Ticket.Status.WAITING, waiting_on=Ticket.WaitingOn.REQUESTER))
    return render(request, "core/employee_home.html", {
        "profile": request.user.profile, "recent": mine.order_by("-created_at")[:8], "needs_action": needs_action,
    })


@role_required(Role.ADMIN)
def system_settings(request):
    obj = SystemSetting.get()
    before = SystemSetting.objects.get(pk=obj.pk)
    if request.method == "POST":
        form = SystemSettingForm(request.POST, instance=SystemSetting.objects.get(pk=obj.pk))
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            changes = audit.diff_instances(before, saved, SETTING_FIELDS)
            if changes:
                audit.log(AuditLog.Action.CONFIG_CHANGE, saved, changes=changes, message="System settings changed")
            messages.success(request, "System configuration saved.")
            return redirect("core:settings")
    else:
        form = SystemSettingForm(instance=obj)
    return render(request, "core/settings.html", {"form": form, "obj": obj})


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@role_required(Role.ADMIN)
def audit_log(request):
    qs = AuditLog.objects.select_related("actor", "content_type")
    q = request.GET.get("q", "").strip()
    action = request.GET.get("action", "")
    start = _parse_date(request.GET.get("start"))
    end = _parse_date(request.GET.get("end"))
    tz = timezone.get_current_timezone()
    if q:
        qs = qs.filter(Q(actor_username__icontains=q) | Q(object_repr__icontains=q) | Q(message__icontains=q)
                       | Q(ip_address__icontains=q))
    if action in AuditLog.Action.values:
        qs = qs.filter(action=action)
    if start:
        qs = qs.filter(timestamp__gte=timezone.make_aware(datetime.combine(start, time.min), tz))
    if end:
        qs = qs.filter(timestamp__lte=timezone.make_aware(datetime.combine(end, time.max), tz))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/audit_log.html", {
        "page_obj": page, "q": q, "action": action, "actions": AuditLog.Action.choices,
        "start": request.GET.get("start", ""), "end": request.GET.get("end", ""),
    })


@login_required
def search(request):
    """Global search across the records the user is allowed to see."""
    from apps.accounts.roles import is_it_staff
    from apps.helpdesk.models import Ticket
    from apps.projects.models import Project

    q = request.GET.get("q", "").strip()[:100]
    groups = []
    if len(q) >= 2:
        user = request.user
        tickets = Ticket.objects.visible_to(user).filter(
            Q(reference__icontains=q) | Q(subject__icontains=q) | Q(description__icontains=q)
            | Q(affected_device__icontains=q)).order_by("-created_at")[:15]
        groups.append(("Tickets", "life-preserver", [(t.reference, t.subject, t.get_status_display(), t.get_absolute_url())
                                                     for t in tickets]))
        projects = Project.objects.visible_to(user).filter(
            Q(code__icontains=q) | Q(name__icontains=q) | Q(description__icontains=q))[:10]
        groups.append(("Projects", "kanban", [(p.code, p.name, p.get_status_display(), p.get_absolute_url()) for p in projects]))
        if is_it_staff(user):
            from apps.assets.models import Asset
            from apps.diary.models import ActivityLog
            from apps.knowledge.matching import search as kb_search
            from apps.knowledge.models import KnowledgeArticle
            from apps.procurement.models import ProcurementRequest
            from apps.tasks.models import Task

            tasks = Task.objects.filter(Q(number__icontains=q) | Q(title__icontains=q) | Q(description__icontains=q)) \
                .order_by("-planned_date")[:15]
            groups.append(("Tasks", "check2-square", [(t.number, t.title, t.get_status_display(), t.get_absolute_url())
                                                      for t in tasks]))
            assets = Asset.objects.filter(Q(tag__icontains=q) | Q(name__icontains=q) | Q(serial_number__icontains=q)
                                          | Q(hostname__icontains=q) | Q(ip_address__icontains=q) | Q(model__icontains=q))[:15]
            groups.append(("Assets", "pc-display", [(a.tag, a.name, a.get_status_display(), a.get_absolute_url())
                                                    for a in assets]))
            articles = kb_search(q, KnowledgeArticle.objects.exclude(status="archived"))[:10]
            groups.append(("Knowledge base", "book", [("✔" if a.is_verified else "", a.title, a.get_status_display(),
                                                       a.get_absolute_url()) for a in articles]))
            purchases = ProcurementRequest.objects.filter(Q(reference__icontains=q) | Q(item__icontains=q))[:10]
            groups.append(("Procurement", "cart3", [(p.reference, p.item, p.get_status_display(), p.get_absolute_url())
                                                    for p in purchases]))
            diary = ActivityLog.objects.current().filter(user=user).filter(
                Q(title__icontains=q) | Q(description__icontains=q) | Q(outcome__icontains=q))[:10]
            groups.append(("My diary", "journal-text", [(timezone.localtime(d.occurred_at).strftime("%d %b %Y"), d.title,
                                                         d.get_source_display(), reverse("diary:detail", args=[d.pk]))
                                                        for d in diary]))
    total = sum(len(items) for _n, _i, items in groups)
    return render(request, "core/search.html", {"q": q, "groups": [g for g in groups if g[2]], "total": total})


def error_403(request, exception=None):
    return render(request, "errors/403.html", status=403)


def error_404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)
