from django import forms
from django.db.models import Q

from apps.accounts.models import Department
from apps.core.forms import BootstrapFormMixin
from apps.evidence.forms import EvidenceMixin, MultipleFileField
from apps.tasks.forms import DATE, it_staff_queryset

from .models import Project, ProjectMilestone

TA = forms.Textarea


class ProjectForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "status", "priority", "owner", "sponsor_department", "start_date", "target_date",
                  "description", "business_objective", "estimated_cost", "other_costs", "currency", "risks", "blockers",
                  "deliverables", "final_outcome", "benefits", "documented_savings", "savings_evidence"]
        widgets = {"start_date": DATE, "target_date": DATE, "description": TA(attrs={"rows": 3}),
                   "business_objective": TA(attrs={"rows": 3}), "risks": TA(attrs={"rows": 2}),
                   "blockers": TA(attrs={"rows": 2}), "deliverables": TA(attrs={"rows": 3}),
                   "final_outcome": TA(attrs={"rows": 3}), "benefits": TA(attrs={"rows": 3}),
                   "savings_evidence": TA(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = it_staff_queryset()
        self.fields["owner"].label_from_instance = lambda u: u.get_full_name() or u.username
        dept = self.instance.sponsor_department_id if self.instance.pk else None
        self.fields["sponsor_department"].queryset = Department.objects.filter(Q(is_active=True) | Q(pk=dept))

    def clean_currency(self):
        return (self.cleaned_data.get("currency") or "").strip().upper()


class MilestoneForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ProjectMilestone
        fields = ["title", "due_date", "notes"]
        widgets = {"due_date": DATE, "notes": TA(attrs={"rows": 2})}


class UpdateForm(BootstrapFormMixin, forms.Form):
    body = forms.CharField(widget=TA(attrs={"rows": 3}), label="Progress update",
                           help_text="What was done, decisions made, new risks or blockers.")


class AttachmentForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    files = MultipleFileField()
    description = forms.CharField(max_length=255, required=False)
    files_label = "Project documents"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()
