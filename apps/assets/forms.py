from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.accounts.models import Department, Location
from apps.core.forms import BootstrapFormMixin
from apps.evidence.forms import EvidenceMixin, MultipleFileField
from apps.tasks.forms import DATE

from .models import Asset, AssetCategory, Supplier

User = get_user_model()


def _limit(form, name, qs):
    current = getattr(form.instance, f"{name}_id", None) if form.instance.pk else None
    form.fields[name].queryset = qs.model.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk=current)) if current else qs


class AssetForm(BootstrapFormMixin, forms.ModelForm):
    note = forms.CharField(max_length=300, required=False, label="Note for the history (optional)",
                           help_text="e.g. 'Moved to the night audit desk'.")

    class Meta:
        model = Asset
        fields = ["name", "category", "manufacturer", "model", "serial_number", "status", "condition",
                  "department", "location", "assigned_to",
                  "purchase_date", "purchase_cost", "currency", "supplier", "warranty_expiry", "warranty_details",
                  "hostname", "ip_address", "mac_address", "operating_system", "os_version", "software",
                  "replacement", "replacement_reason", "notes"]
        widgets = {"purchase_date": DATE, "warranty_expiry": DATE,
                   "warranty_details": forms.Textarea(attrs={"rows": 2}), "software": forms.Textarea(attrs={"rows": 3}),
                   "replacement_reason": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _limit(self, "category", AssetCategory.objects.filter(is_active=True))
        _limit(self, "department", Department.objects.active())
        _limit(self, "location", Location.objects.active())
        _limit(self, "supplier", Supplier.objects.filter(is_active=True))
        _limit(self, "assigned_to", User.objects.filter(is_active=True).order_by("first_name", "last_name"))
        self.fields["assigned_to"].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields["currency"].help_text = "Three-letter code; defaults to the hotel currency."
        # Retiring / disposing has its own form so a reason is recorded.
        out = (Asset.Status.RETIRED, Asset.Status.DISPOSED, Asset.Status.LOST)
        if not self.instance.pk or self.instance.status not in out:
            self.fields["status"].choices = [c for c in Asset.Status.choices if c[0] not in out]
        if self.instance.pk:
            self.fields["category"].disabled = True
            self.fields["category"].help_text = "The category is part of the asset ID and cannot be changed."
        else:
            del self.fields["note"]

    def clean_currency(self):
        from apps.core.models import SystemSetting

        value = (self.cleaned_data.get("currency") or "").strip().upper()
        if not value and self.cleaned_data.get("purchase_cost") is not None:
            value = SystemSetting.get().currency  # default to the hotel's currency
        if value and not (len(value) == 3 and value.isalpha()):
            raise forms.ValidationError("Use a three-letter currency code, e.g. KES, USD, EUR.")
        return value

    def clean_serial_number(self):
        return self.cleaned_data["serial_number"].strip()

    def clean(self):
        cleaned = super().clean()
        serial, maker = cleaned.get("serial_number"), cleaned.get("manufacturer", "")
        if serial and Asset.objects.filter(serial_number__iexact=serial, manufacturer=maker).exclude(pk=self.instance.pk).exists():
            self.add_error("serial_number", "Another asset from this manufacturer already has this serial number.")
        return cleaned


class StatusForm(BootstrapFormMixin, forms.Form):
    status = forms.ChoiceField(choices=Asset.Status.choices)
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class AttachmentForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    files = MultipleFileField()
    description = forms.CharField(max_length=255, required=False)
    files_label = "Documents (invoice, warranty, manual, photos)"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()


class AssetCategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = AssetCategory
        fields = ["name", "code", "default_maintenance_days", "expected_life_years", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.assets.exists():
            self.fields["code"].disabled = True
            self.fields["code"].help_text = "Assets already use this code in their IDs."

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()


class SupplierForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "contact_person", "phone", "email", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
