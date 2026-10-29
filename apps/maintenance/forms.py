from django import forms
from django.utils import timezone

from apps.assets.models import Asset
from apps.core.forms import BootstrapFormMixin
from apps.evidence.forms import EvidenceMixin, MultipleFileField
from apps.tasks.forms import DATE, it_staff_queryset

from .models import MaintenanceAttachment, MaintenancePlan


class PlanForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = MaintenancePlan
        fields = ["asset", "title", "interval_value", "interval_unit", "next_due_date", "lead_days", "responsible",
                  "estimated_minutes", "checklist", "is_active"]
        widgets = {"next_due_date": DATE, "checklist": forms.Textarea(attrs={"rows": 6})}
        labels = {"next_due_date": "Next due date", "estimated_minutes": "Estimated duration (minutes)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].queryset = Asset.objects.in_service().select_related("category")
        self.fields["responsible"].queryset = it_staff_queryset()
        self.fields["responsible"].label_from_instance = lambda u: u.get_full_name() or u.username
        if self.instance.pk:
            self.fields["asset"].disabled = True
        self.fields["checklist"].widget.attrs["placeholder"] = "Clean dust filters\nCheck fans and temperatures\nCheck disk health\nApply firmware updates"


class CompleteForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    """Checklist fields are added dynamically: one result + note per plan checklist line."""

    completed_on = forms.DateField(widget=DATE, label="Date done")
    work_done = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Work done")
    inspection_results = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    problems_found = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    parts_used = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    cost = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, label="Cost (parts / external service)")
    currency = forms.CharField(max_length=3, required=False)
    recommendations = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    asset_condition = forms.ChoiceField(choices=[("", "— unchanged —")] + list(Asset.Condition.choices), required=False,
                                        label="Asset condition afterwards")
    files = MultipleFileField()
    files_label = "After-the-work photos / reports"

    RESULTS = [("ok", "OK"), ("issue", "Issue"), ("na", "N/A")]

    def __init__(self, *args, checklist=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()
        self.checklist = list(checklist)
        self.fields["completed_on"].initial = timezone.localdate()
        for i, item in enumerate(self.checklist):
            self.fields[f"check_{i}"] = forms.ChoiceField(choices=self.RESULTS, label=item, widget=forms.RadioSelect,
                                                          initial="ok")
            self.fields[f"check_{i}"].widget.attrs["class"] = "form-check-input"
            self.fields[f"note_{i}"] = forms.CharField(required=False, label="Note", max_length=300)
            self.fields[f"note_{i}"].widget.attrs["class"] = "form-control form-control-sm"

    def checklist_rows(self):
        return [(self[f"check_{i}"], self[f"note_{i}"]) for i in range(len(self.checklist))]

    def checklist_results(self):
        return [{"item": item, "result": self.cleaned_data[f"check_{i}"], "note": self.cleaned_data[f"note_{i}"]}
                for i, item in enumerate(self.checklist)]

    def clean(self):
        cleaned = super().clean()
        for i in range(len(self.checklist)):
            if cleaned.get(f"check_{i}") == "issue" and not cleaned.get(f"note_{i}"):
                self.add_error(f"note_{i}", "Describe the issue.")
        return cleaned


class CorrectiveForm(CompleteForm):
    title = forms.CharField(max_length=150, label="Repair", help_text="e.g. 'Replaced power supply'.")
    asset_status = forms.ChoiceField(choices=[("", "— unchanged —"), ("in_use", "In use"), ("in_stock", "In stock / spare"),
                                              ("under_repair", "Still under repair")], required=False,
                                     label="Asset status afterwards")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(["title", "completed_on", "problems_found", "work_done", "parts_used", "cost", "currency",
                           "asset_condition", "asset_status", "recommendations", "files"])
        del self.fields["inspection_results"]


class RescheduleForm(BootstrapFormMixin, forms.Form):
    new_date = forms.DateField(widget=DATE, label="New due date")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class ReasonForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class EvidenceForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    files = MultipleFileField()
    stage = forms.ChoiceField(choices=MaintenanceAttachment.Stage.choices)
    description = forms.CharField(max_length=255, required=False)
    files_label = "Before / after evidence"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()
