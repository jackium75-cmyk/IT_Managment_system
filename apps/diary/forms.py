from django import forms
from django.utils import timezone

from apps.accounts.models import Department, Location
from apps.core.forms import BootstrapFormMixin

from .models import ActivityLog


class ManualEntryForm(BootstrapFormMixin, forms.ModelForm):
    occurred_at = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"], label="Started",
    )

    class Meta:
        model = ActivityLog
        fields = ["occurred_at", "duration_minutes", "activity_type", "title", "description", "outcome",
                  "department", "location"]
        labels = {"duration_minutes": "Duration (minutes)", "title": "What did you do?"}
        widgets = {"description": forms.Textarea(attrs={"rows": 3}), "outcome": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["duration_minutes"].required = True
        self.fields["department"].queryset = Department.objects.active()
        self.fields["location"].queryset = Location.objects.active()
        self.fields["description"].help_text = (
            "Use this for work that did not come from a task or ticket — meetings, consultations, "
            "training, documentation. Time on tasks is recorded on the task itself."
        )

    def clean_occurred_at(self):
        value = self.cleaned_data["occurred_at"]
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        if value > timezone.now():
            raise forms.ValidationError("You cannot record work that has not started yet.")
        return value


class VoidEntryForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, label="Reason for voiding")
