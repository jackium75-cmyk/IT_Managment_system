from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.accounts.models import Department
from apps.assets.models import Asset
from apps.core.forms import BootstrapFormMixin
from apps.diary.choices import ActivityType
from apps.helpdesk.models import Ticket, TicketCategory
from apps.projects.models import Project
from apps.tasks.forms import DATE, it_staff_queryset
from apps.tasks.models import Priority, Task, TaskCategory

KINDS = [("daily", "Daily IT activity"), ("weekly", "Weekly IT report"), ("monthly", "Monthly performance"),
         ("annual", "Annual IT report"), ("custom", "Custom report")]


def status_choices():
    seen, out = set(), [("", "Any")]
    for value, label in list(Task.Status.choices) + list(Ticket.Status.choices):
        if value not in seen:
            seen.add(value)
            out.append((value, label))
    return out


class ReportForm(BootstrapFormMixin, forms.Form):
    kind = forms.ChoiceField(choices=KINDS, label="Report")
    start = forms.DateField(widget=DATE, required=False, label="From")
    end = forms.DateField(widget=DATE, required=False, label="To")
    department = forms.ModelChoiceField(queryset=Department.objects.none(), required=False)
    task_category = forms.ModelChoiceField(queryset=TaskCategory.objects.all(), required=False)
    ticket_category = forms.ModelChoiceField(queryset=TicketCategory.objects.all(), required=False)
    priority = forms.ChoiceField(choices=[("", "Any")] + list(Priority.choices), required=False)
    status = forms.ChoiceField(choices=(), required=False)
    technician = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, label="IT technician")
    asset = forms.ModelChoiceField(queryset=Asset.objects.all(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    activity_type = forms.ChoiceField(choices=[("", "Any")] + list(ActivityType.choices), required=False)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False, max_length=3000,
                            label="Your commentary and recommendations (optional)",
                            help_text="Included in the report as your own words, separate from figures calculated by the system.")

    def __init__(self, *args, user=None, manager=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = status_choices()
        self.fields["technician"].queryset = it_staff_queryset()
        self.fields["technician"].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields["ticket_category"].label_from_instance = str
        if manager:
            dept = user.profile.department_id
            self.fields["department"].queryset = Department.objects.filter(pk=dept)
            self.fields["department"].disabled = True
            self.fields["department"].initial = dept
            self.fields["project"].queryset = Project.objects.visible_to(user)
            self.fields["asset"].queryset = Asset.objects.filter(department_id=dept)
            for name in ("task_category", "activity_type"):
                del self.fields[name]
        else:
            self.fields["department"].queryset = Department.objects.filter(Q(is_active=True))
