from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, SetPasswordForm
from django.db import transaction

from apps.core import audit
from apps.core.forms import BootstrapFormMixin
from apps.core.models import AuditLog

from . import throttling
from .models import Department, Location, UserProfile
from .roles import Role

User = get_user_model()


class ThrottledAuthenticationForm(BootstrapFormMixin, AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        "locked": "Too many failed sign-in attempts. Please wait %(minutes)s minutes or contact IT support.",
    }

    def clean(self):
        username = self.cleaned_data.get("username")
        ip = audit.client_ip(self.request)
        if username and throttling.is_locked(username, ip):
            audit.log(AuditLog.Action.LOGIN_LOCKED, request=self.request, object_repr=throttling.normalise(username),
                      message="Sign-in blocked by lockout")
            raise forms.ValidationError(
                self.error_messages["locked"], code="locked", params={"minutes": throttling.lockout_minutes()}
            )
        try:
            cleaned = super().clean()
        except forms.ValidationError:
            if username:
                throttling.record(username, ip, successful=False)
                audit.log(AuditLog.Action.LOGIN_FAILED, request=self.request,
                          object_repr=throttling.normalise(username), message="Invalid credentials or inactive account")
            raise
        throttling.record(username, ip, successful=True)
        return cleaned


class StyledPasswordChangeForm(BootstrapFormMixin, PasswordChangeForm):
    pass


class AdminSetPasswordForm(BootstrapFormMixin, SetPasswordForm):
    force_change = forms.BooleanField(
        required=False, initial=True, label="Require the user to change this password at next sign-in"
    )


PROFILE_FIELDS = ["role", "department", "location", "job_title", "phone", "extension"]


class _UserProfileFieldsMixin(forms.Form):
    role = forms.ChoiceField(choices=Role.choices)
    department = forms.ModelChoiceField(queryset=Department.objects.active(), required=False)
    location = forms.ModelChoiceField(queryset=Location.objects.active(), required=False)
    job_title = forms.CharField(max_length=120, required=False)
    phone = forms.CharField(max_length=40, required=False)
    extension = forms.CharField(max_length=10, required=False)

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        if role in (Role.MANAGER, Role.EMPLOYEE) and not cleaned.get("department"):
            self.add_error("department", "Managers and employees must belong to a department.")
        return cleaned


class UserCreateForm(BootstrapFormMixin, _UserProfileFieldsMixin, forms.ModelForm):
    password1 = forms.CharField(label="Temporary password", widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput, strip=False)

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords do not match.")
        if p1:
            try:
                password_validation.validate_password(p1, User(username=cleaned.get("username", "")))
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        user.save()
        profile = user.profile
        for f in PROFILE_FIELDS:
            setattr(profile, f, self.cleaned_data.get(f) or ("" if f in ("job_title", "phone", "extension") else None))
        profile.must_change_password = True
        profile.save()
        return user


class UserEditForm(BootstrapFormMixin, _UserProfileFieldsMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            profile = self.instance.profile
            for f in PROFILE_FIELDS:
                self.fields[f].initial = getattr(profile, f)
            # Allow keeping an inactive department/location already assigned.
            self.fields["department"].queryset = Department.objects.filter(
                pk__in=[profile.department_id] if profile.department_id else []
            ) | Department.objects.active()
            self.fields["location"].queryset = Location.objects.filter(
                pk__in=[profile.location_id] if profile.location_id else []
            ) | Location.objects.active()

    @transaction.atomic
    def save(self, commit=True):
        user = super().save()
        profile = user.profile
        for f in PROFILE_FIELDS:
            setattr(profile, f, self.cleaned_data.get(f) or ("" if f in ("job_title", "phone", "extension") else None))
        profile.save()
        return user


class OwnProfileForm(BootstrapFormMixin, forms.ModelForm):
    """Fields users may change themselves. Role and department are admin-only."""

    class Meta:
        model = UserProfile
        fields = ["phone", "extension", "location", "email_notifications"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = Location.objects.active()


class DepartmentForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "code", "description", "head", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["head"].queryset = User.objects.filter(is_active=True).order_by("first_name", "last_name")
        self.fields["head"].label_from_instance = lambda u: u.get_full_name() or u.username

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Department.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A department with this name already exists.")
        return name


class LocationForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Location
        fields = ["name", "building", "floor", "department", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        qs = Location.objects.filter(
            name__iexact=cleaned.get("name", ""), building__iexact=cleaned.get("building", ""),
            floor__iexact=cleaned.get("floor", ""),
        ).exclude(pk=self.instance.pk)
        if cleaned.get("name") and qs.exists():
            raise forms.ValidationError("This location already exists.")
        return cleaned
