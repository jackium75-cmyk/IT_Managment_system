from django import forms

from .models import SystemSetting


class BootstrapFormMixin:
    """
    Apply Bootstrap 5 classes to every widget and mark invalid fields.

    Invalid-field marking happens in ``full_clean`` — never in ``__init__``,
    because reading ``self.errors`` there would validate the form before
    subclasses finish setting it up (restricting choices, preparing uploads).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            # ``render_kind`` tells templates/includes/field.html how to lay the field out.
            if isinstance(widget, (forms.RadioSelect, forms.CheckboxSelectMultiple)):
                widget.render_kind = "choices"
                css = "form-check-input"
            elif isinstance(widget, forms.CheckboxInput):
                widget.render_kind = "check"
                css = "form-check-input"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = "form-select"
            else:
                css = "form-control"
            if not hasattr(widget, "render_kind"):
                widget.render_kind = "input"
            if css == "form-check-input" and widget.render_kind == "choices":
                widget.attrs["class"] = css
                continue
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()

    def full_clean(self):
        super().full_clean()
        for name in self._errors or {}:
            if name in self.fields:
                widget = self.fields[name].widget
                if "is-invalid" not in widget.attrs.get("class", ""):
                    widget.attrs["class"] = f"{widget.attrs.get('class', '')} is-invalid".strip()


class SystemSettingForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = [
            "hotel_name", "hotel_timezone", "it_department_email", "it_department_phone",
            "max_upload_size_mb", "allowed_upload_extensions",
            "login_max_failures", "login_lockout_minutes", "email_notifications_enabled",
            "ticket_auto_close_days", "ticket_reopen_days", "currency", "maintenance_grace_days",
            "procurement_requires_approval", "procurement_approval_threshold",
        ]

    def clean_allowed_upload_extensions(self):
        from apps.evidence.validation import BLOCKED_EXTENSIONS, KNOWN_TYPES

        raw = self.cleaned_data["allowed_upload_extensions"]
        exts = [e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()]
        # Executable and script types are never permitted, whatever is typed here.
        bad = sorted(set(exts) & BLOCKED_EXTENSIONS)
        if bad:
            raise forms.ValidationError(f"These file types are not allowed for security reasons: {', '.join(bad)}")
        unknown = sorted(set(exts) - set(KNOWN_TYPES))
        if unknown:
            raise forms.ValidationError(
                f"The system cannot verify these file types, so they cannot be enabled: {', '.join(unknown)}. "
                f"Supported: {', '.join(sorted(KNOWN_TYPES))}.")
        if not exts:
            raise forms.ValidationError("At least one file type must be allowed.")
        return ",".join(sorted(set(exts)))

    def clean_currency(self):
        value = self.cleaned_data["currency"].strip().upper()
        if not (len(value) == 3 and value.isalpha()):
            raise forms.ValidationError("Use a three-letter currency code, e.g. KES, USD, EUR.")
        return value
