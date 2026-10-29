from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.roles import IT_STAFF_ROLES, Role, is_it_staff
from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required
from apps.diary.timecalc import format_minutes
from apps.evidence import serving
from apps.tasks.views import _apply_errors

from . import services
from .forms import AttachmentForm, MilestoneForm, ProjectForm, UpdateForm
from .models import Project, ProjectAttachment, ProjectMilestone

it_required = role_required(*IT_STAFF_ROLES)
viewer_required = role_required(Role.ADMIN, Role.IT_OFFICER, Role.MANAGER)


def _get(request, pk):
    project = get_object_or_404(Project.objects.select_related("owner", "sponsor_department", "benefits_verified_by"), pk=pk)
    if not services.can_view(request.user, project):
        audit.log(AuditLog.Action.ACCESS_DENIED, project, message="Attempted to open a project without permission")
        raise PermissionDenied("This project is not shared with your department.")
    return project


@viewer_required
def project_list(request):
    qs = Project.objects.visible_to(request.user).select_related("owner", "sponsor_department")
    status = request.GET.get("status", "active")
    q = request.GET.get("q", "").strip()
    if status == "active":
        qs = qs.active()
    elif status in Project.Status.values:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(description__icontains=q))
    page = Paginator(qs.order_by("target_date", "-created_at"), 25).get_page(request.GET.get("page"))
    return render(request, "projects/project_list.html", {"page_obj": page, "status": status, "q": q,
                                                          "statuses": Project.Status.choices, "it": is_it_staff(request.user)})


@it_required
def project_edit(request, pk=None):
    project = get_object_or_404(Project, pk=pk) if pk else None
    before = Project.objects.get(pk=pk) if pk else None
    if request.method == "POST":
        form = ProjectForm(request.POST, instance=project)
        if form.is_valid():
            try:
                obj = form.save(commit=False)
                obj = services.update_project(obj, before, request.user) if project else services.create_project(obj, request.user)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Project {obj.code} saved.")
                return redirect(obj)
    else:
        form = ProjectForm(instance=project, initial=None if project else {"owner": request.user.pk})
    return render(request, "projects/project_form.html", {"form": form, "project": project})


@viewer_required
@require_POST
def confirm_benefits(request, pk):
    project = _get(request, pk)
    try:
        services.confirm_benefits(project, request.user)
        messages.success(request, "Thank you. The benefits are now shown as confirmed by you in reports.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(project)


@viewer_required
def project_detail(request, pk):
    project = _get(request, pk)
    it = is_it_staff(request.user)
    return render(request, "projects/project_detail.html", {
        "project": project, "it": it, "milestones": project.milestones.all(),
        "can_confirm": services.can_confirm_benefits(request.user, project) and not project.benefits_verified_by_id,
        "updates": project.updates.select_related("author")[:50], "work": services.linked_work(project),
        "time": format_minutes(services.time_minutes(project)), "costs": services.costs(project),
        "attachments": project.attachments.filter(is_deleted=False),
        "milestone_form": MilestoneForm(), "update_form": UpdateForm(), "attach_form": AttachmentForm(),
    })


@viewer_required
def project_report(request, pk):
    """Printable completion / status report — only recorded facts, clearly labelled."""
    project = _get(request, pk)
    audit.log(AuditLog.Action.REPORT, project, message="Project report viewed")
    return render(request, "projects/project_report.html", {
        "project": project, "milestones": project.milestones.all(), "work": services.linked_work(project),
        "time": format_minutes(services.time_minutes(project)), "costs": services.costs(project),
        "updates": project.updates.filter(is_system=False).select_related("author")[:20],
    })


@it_required
@require_POST
def milestone_add(request, pk):
    project = _get(request, pk)
    form = MilestoneForm(request.POST)
    if form.is_valid():
        m = form.save(commit=False)
        m.project = project
        m.order = project.milestones.count()
        services.save_milestone(m, request.user)
        messages.success(request, "Milestone added.")
    else:
        messages.error(request, "Give the milestone a title.")
    return redirect(project)


@it_required
@require_POST
def milestone_toggle(request, pk):
    m = get_object_or_404(ProjectMilestone, pk=pk)
    services.toggle_milestone(m, request.user)
    return redirect(m.project)


@it_required
@require_POST
def update_add(request, pk):
    project = _get(request, pk)
    form = UpdateForm(request.POST)
    if form.is_valid():
        services.add_update(project, request.user, form.cleaned_data["body"])
        messages.success(request, "Progress update recorded.")
    else:
        messages.error(request, "Write the update first.")
    return redirect(project)


@it_required
@require_POST
def attach(request, pk):
    project = _get(request, pk)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid() and form.uploads:
        services.add_attachments(project, form.uploads, request.user, form.cleaned_data["description"])
        messages.success(request, f"{len(form.uploads)} document(s) added.")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        if form.is_valid():
            messages.error(request, "Choose at least one file.")
    return redirect(project)


@login_required
def attachment_view(request, pk):
    att = get_object_or_404(ProjectAttachment.objects.select_related("project"), pk=pk)
    if att.is_deleted or not services.can_view(request.user, att.project):
        raise PermissionDenied
    return serving.serve(request, att, download=request.GET.get("download") == "1")
