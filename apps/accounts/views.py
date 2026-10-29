from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash, views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.models import AuditLog
from apps.core.permissions import role_required

from .forms import (
    AdminSetPasswordForm, DepartmentForm, LocationForm, OwnProfileForm, StyledPasswordChangeForm,
    ThrottledAuthenticationForm, UserCreateForm, UserEditForm,
)
from .models import Department, Location
from .roles import Role

User = get_user_model()
PAGE_SIZE = 25

USER_AUDIT_FIELDS = ["first_name", "last_name", "email", "is_active"]
PROFILE_AUDIT_FIELDS = ["role", "department", "location", "job_title", "phone", "extension"]


# ---------------------------------------------------------------------------
# Sign in / out / password
# ---------------------------------------------------------------------------
class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = ThrottledAuthenticationForm
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    """POST-only sign-out (Django default), protected by CSRF."""


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "accounts/password_change.html"
    form_class = StyledPasswordChangeForm
    success_url = reverse_lazy("core:home")

    def form_valid(self, form):
        response = super().form_valid(form)
        profile = self.request.user.profile
        if profile.must_change_password:
            profile.must_change_password = False
            profile.save(update_fields=["must_change_password", "updated_at"])
        audit.log(AuditLog.Action.PASSWORD_CHANGE, self.request.user, message="User changed own password")
        messages.success(self.request, "Your password has been changed.")
        return response


@login_required
def my_profile(request):
    profile = request.user.profile
    if request.method == "POST":
        before = {f: getattr(profile, f) for f in ("phone", "extension", "location", "email_notifications")}
        form = OwnProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            changes = {f: [str(before[f] or ""), str(getattr(profile, f) or "")]
                       for f in before if before[f] != getattr(profile, f)}
            if changes:
                audit.log(AuditLog.Action.UPDATE, request.user, changes=changes, message="Updated own profile")
            messages.success(request, "Your profile has been updated.")
            return redirect("accounts:profile")
    else:
        form = OwnProfileForm(instance=profile)
    return render(request, "accounts/profile.html", {"form": form, "profile": profile})


# ---------------------------------------------------------------------------
# User administration (System Administrator only)
# ---------------------------------------------------------------------------
@role_required(Role.ADMIN)
def user_list(request):
    qs = User.objects.select_related("profile", "profile__department").order_by("username")
    q = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    department = request.GET.get("department", "")
    status = request.GET.get("status", "active")
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                       | Q(email__icontains=q) | Q(profile__job_title__icontains=q))
    if role in Role.values:
        qs = qs.filter(profile__role=role)
    if department.isdigit():
        qs = qs.filter(profile__department_id=int(department))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "accounts/user_list.html", {
        "page_obj": page, "q": q, "role": role, "department": department, "status": status,
        "roles": Role.choices, "departments": Department.objects.order_by("name"),
    })


@role_required(Role.ADMIN)
def user_create(request):
    if request.method == "POST":
        form = UserCreateForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                audit.log(AuditLog.Action.CREATE, user, message=f"Created account with role {user.profile.get_role_display()}")
            messages.success(request, f"User '{user.username}' created. They must change the temporary password at first sign-in.")
            return redirect("accounts:user_detail", pk=user.pk)
    else:
        form = UserCreateForm()
    return render(request, "accounts/user_form.html", {"form": form, "creating": True})


@role_required(Role.ADMIN)
def user_detail(request, pk):
    obj = get_object_or_404(User.objects.select_related("profile"), pk=pk)
    history = AuditLog.objects.filter(
        Q(content_type__model="user", object_id=str(obj.pk)) | Q(actor=obj)
    ).select_related("actor")[:25]
    return render(request, "accounts/user_detail.html", {"obj": obj, "history": history})


@role_required(Role.ADMIN)
def user_edit(request, pk):
    obj = get_object_or_404(User.objects.select_related("profile"), pk=pk)
    before_user = User.objects.get(pk=pk)
    before_profile = obj.profile.__class__.objects.get(pk=obj.profile.pk)
    if request.method == "POST":
        form = UserEditForm(request.POST, instance=obj)
        if form.is_valid():
            if obj == request.user and not form.cleaned_data["is_active"]:
                form.add_error("is_active", "You cannot deactivate your own account.")
            elif obj == request.user and form.cleaned_data["role"] != Role.ADMIN and not obj.is_superuser:
                form.add_error("role", "You cannot remove your own administrator role.")
            else:
                with transaction.atomic():
                    user = form.save()
                    changes = audit.diff_instances(before_user, user, USER_AUDIT_FIELDS)
                    changes.update(audit.diff_instances(before_profile, user.profile, PROFILE_AUDIT_FIELDS))
                    if changes:
                        action = AuditLog.Action.UPDATE
                        if "is_active" in changes:
                            action = AuditLog.Action.ACTIVATE if user.is_active else AuditLog.Action.DEACTIVATE
                        audit.log(action, user, changes=changes, message="Account updated by administrator")
                messages.success(request, f"User '{user.username}' updated.")
                return redirect("accounts:user_detail", pk=user.pk)
    else:
        form = UserEditForm(instance=obj)
    return render(request, "accounts/user_form.html", {"form": form, "obj": obj, "creating": False})


@role_required(Role.ADMIN)
def user_set_password(request, pk):
    obj = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = AdminSetPasswordForm(obj, request.POST)
        if form.is_valid():
            form.save()
            obj.profile.must_change_password = form.cleaned_data["force_change"]
            obj.profile.save(update_fields=["must_change_password", "updated_at"])
            if obj == request.user:
                update_session_auth_hash(request, obj)
            audit.log(AuditLog.Action.PASSWORD_RESET, obj, message="Password reset by administrator")
            messages.success(request, f"Password for '{obj.username}' has been reset.")
            return redirect("accounts:user_detail", pk=obj.pk)
    else:
        form = AdminSetPasswordForm(obj)
    return render(request, "accounts/user_set_password.html", {"form": form, "obj": obj})


@role_required(Role.ADMIN)
@require_POST
def user_toggle_active(request, pk):
    obj = get_object_or_404(User, pk=pk)
    if obj == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("accounts:user_detail", pk=obj.pk)
    obj.is_active = not obj.is_active
    obj.save(update_fields=["is_active"])
    audit.log(AuditLog.Action.ACTIVATE if obj.is_active else AuditLog.Action.DEACTIVATE, obj,
              changes={"is_active": [not obj.is_active, obj.is_active]})
    messages.success(request, f"User '{obj.username}' {'activated' if obj.is_active else 'deactivated'}.")
    return redirect("accounts:user_detail", pk=obj.pk)


# ---------------------------------------------------------------------------
# Departments and locations (System Administrator only)
# ---------------------------------------------------------------------------
def _simple_list(request, model, template, extra_qs=None):
    qs = model.objects.all()
    if extra_qs:
        qs = extra_qs(qs)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active")
    if q:
        qs = qs.filter(name__icontains=q)
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, template, {"page_obj": page, "q": q, "status": status})


def _simple_edit(request, model, form_class, pk, list_url, label, audit_fields):
    obj = get_object_or_404(model, pk=pk) if pk else None
    before = model.objects.get(pk=pk) if pk else None
    if request.method == "POST":
        form = form_class(request.POST, instance=obj)
        if form.is_valid():
            with transaction.atomic():
                saved = form.save()
                if obj is None:
                    audit.log(AuditLog.Action.CREATE, saved, message=f"{label} created")
                else:
                    changes = audit.diff_instances(before, saved, audit_fields)
                    if changes:
                        action = AuditLog.Action.UPDATE
                        if list(changes) == ["is_active"]:
                            action = AuditLog.Action.ACTIVATE if saved.is_active else AuditLog.Action.DEACTIVATE
                        audit.log(action, saved, changes=changes, message=f"{label} updated")
            messages.success(request, f"{label} '{saved}' saved.")
            return redirect(list_url)
    else:
        form = form_class(instance=obj)
    return render(request, "accounts/simple_form.html", {
        "form": form, "obj": obj, "label": label, "list_url": list_url,
    })


@role_required(Role.ADMIN)
def department_list(request):
    return _simple_list(
        request, Department, "accounts/department_list.html",
        lambda qs: qs.select_related("head").annotate(
            member_count=Count("members", filter=Q(members__user__is_active=True))
        ).order_by("name"),
    )


@role_required(Role.ADMIN)
def department_edit(request, pk=None):
    return _simple_edit(request, Department, DepartmentForm, pk, "accounts:department_list", "Department",
                        ["name", "code", "description", "head", "is_active"])


@role_required(Role.ADMIN)
def location_list(request):
    return _simple_list(request, Location, "accounts/location_list.html",
                        lambda qs: qs.select_related("department"))


@role_required(Role.ADMIN)
def location_edit(request, pk=None):
    return _simple_edit(request, Location, LocationForm, pk, "accounts:location_list", "Location",
                        ["name", "building", "floor", "department", "description", "is_active"])
