from datetime import datetime, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Department, Location
from apps.accounts.roles import IT_STAFF_ROLES
from apps.core.forms import BootstrapFormMixin
from apps.evidence.forms import EvidenceMixin, MultipleFileField

from .models import RecurringTask, Task, TaskCategory

User = get_user_model()

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
TEXT3 = forms.Textarea(attrs={"rows": 3})


def it_staff_queryset():
    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(profile__role__in=IT_STAFF_ROLES)
    ).order_by("first_name", "last_name", "username")


def _user_label(u):
    return u.get_full_name() or u.username


class _TaskRelationsMixin:
    """Limit choices to active records (keeping any inactive value already set)."""

    def _limit(self, name, qs):
        current = getattr(self.instance, f"{name}_id", None) if self.instance and self.instance.pk else None
        if current:
            qs = qs.model.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk=current))
        self.fields[name].queryset = qs

    def setup_relations(self):
        self._limit("category", TaskCategory.objects.filter(is_active=True))
        self._limit("department", Department.objects.active())
        self._limit("location", Location.objects.active())
        self._limit("assigned_to", it_staff_queryset())
        self.fields["assigned_to"].label_from_instance = _user_label
        if "asset" in self.fields:
            from apps.assets.models import Asset

            self._limit("asset", Asset.objects.in_service())
            self.fields["asset"].required = False
        if "project" in self.fields:
            from apps.projects.models import Project

            self._limit("project", Project.objects.exclude(status__in=["completed", "cancelled"]))


class TaskForm(BootstrapFormMixin, _TaskRelationsMixin, forms.ModelForm):
    """Create a planned task. The schedule of an existing task changes only through Reschedule."""

    class Meta:
        model = Task
        fields = ["title", "category", "priority", "planned_date", "scheduled_start", "scheduled_end",
                  "estimated_minutes", "assigned_to", "department", "location", "asset", "project", "description",
                  "expected_outcome", "related_ticket"]
        widgets = {"planned_date": DATE, "scheduled_start": TIME, "scheduled_end": TIME,
                   "related_ticket": forms.HiddenInput,
                   "description": TEXT3, "expected_outcome": forms.Textarea(attrs={"rows": 2})}
        labels = {"estimated_minutes": "Estimated duration (minutes)"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_relations()
        if not self.instance.pk:
            self.fields["planned_date"].initial = timezone.localdate()
            if user is not None:
                self.fields["assigned_to"].initial = user.pk

    def clean_planned_date(self):
        value = self.cleaned_data["planned_date"]
        if value < timezone.localdate() - timedelta(days=31):
            raise forms.ValidationError("Planned date is more than a month in the past. Record past work in the diary.")
        return value


class TaskEditForm(BootstrapFormMixin, _TaskRelationsMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "category", "priority", "estimated_minutes", "assigned_to", "department", "location",
                  "asset", "project", "description", "expected_outcome", "follow_up_action"]
        widgets = {"description": TEXT3, "expected_outcome": forms.Textarea(attrs={"rows": 2}),
                   "follow_up_action": forms.Textarea(attrs={"rows": 2})}
        labels = {"estimated_minutes": "Estimated duration (minutes)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_relations()


class StatusForm(BootstrapFormMixin, forms.Form):
    status = forms.ChoiceField(choices=[c for c in Task.Status.choices if c[0] != Task.Status.COMPLETED],
                               label="New status")
    reason = forms.CharField(widget=TEXT3, required=False,
                             help_text="Required for waiting, blocked, cancelled and reopening.")
    follow_up_action = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class CompleteForm(BootstrapFormMixin, forms.Form):
    completion_notes = forms.CharField(widget=TEXT3, label="What was done / result")
    follow_up_action = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class RescheduleForm(BootstrapFormMixin, forms.Form):
    new_date = forms.DateField(widget=DATE, label="New date")
    new_start = forms.TimeField(widget=TIME, required=False, label="Start time")
    new_end = forms.TimeField(widget=TIME, required=False, label="End time")
    reason = forms.CharField(widget=TEXT3, help_text="Why the task could not be done as planned.")


class BulkRescheduleForm(BootstrapFormMixin, forms.Form):
    new_date = forms.DateField(widget=DATE, label="Move to")
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))


class CommentForm(BootstrapFormMixin, forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), label="Comment")


class LogTimeForm(BootstrapFormMixin, forms.Form):
    started_at = forms.DateTimeField(widget=DATETIME, label="Started", input_formats=["%Y-%m-%dT%H:%M"])
    minutes = forms.IntegerField(min_value=1, max_value=24 * 60, label="Duration (minutes)")
    note = forms.CharField(max_length=300, required=False)

    def clean_started_at(self):
        value = self.cleaned_data["started_at"]
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value


class VoidForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, label="Reason for voiding")


class UnplannedForm(BootstrapFormMixin, _TaskRelationsMixin, forms.ModelForm):
    """Quick capture of emergency / walk-up work."""

    MODE_TIMER = "timer"
    MODE_DONE = "done"
    MODE_LATER = "later"

    mode = forms.ChoiceField(
        choices=[(MODE_TIMER, "I'm starting it now (start timer)"),
                 (MODE_DONE, "Already finished — record time and result"),
                 (MODE_LATER, "Just record it for now")],
        widget=forms.RadioSelect, initial=MODE_TIMER, label="Status",
    )
    worked_from = forms.DateTimeField(widget=DATETIME, required=False, label="Started at",
                                      input_formats=["%Y-%m-%dT%H:%M"])
    worked_minutes = forms.IntegerField(min_value=1, max_value=24 * 60, required=False, label="Duration (minutes)")
    completion_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                                       label="What was done / result")
    interrupted_task = forms.ModelChoiceField(queryset=Task.objects.none(), required=False,
                                              label="Interrupted task (optional)")

    class Meta:
        model = Task
        fields = ["title", "category", "priority", "assigned_to", "department", "location", "asset", "description"]
        widgets = {"description": TEXT3}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_relations()
        self.fields["assigned_to"].initial = user.pk if user else None
        today = timezone.localdate()
        self.fields["interrupted_task"].queryset = Task.objects.open().filter(
            planned_date__lte=today, assigned_to=user, is_unplanned=False).order_by("-planned_date")
        self.fields["interrupted_task"].label_from_instance = lambda t: f"{t.number} — {t.title}"
        self.fields["mode"].widget.attrs["class"] = "form-check-input"
        self.order_fields(["title", "category", "priority", "interrupted_task", "assigned_to", "department",
                           "location", "asset", "description", "mode", "worked_from", "worked_minutes", "completion_notes"])

    def clean_worked_from(self):
        value = self.cleaned_data.get("worked_from")
        if value and timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mode") == self.MODE_DONE:
            for f in ("worked_from", "worked_minutes", "completion_notes"):
                if not cleaned.get(f):
                    self.add_error(f, "Required when the work is already finished.")
        return cleaned


WEEKDAY_CHOICES = RecurringTask.WEEKDAYS


class RecurringTaskForm(BootstrapFormMixin, _TaskRelationsMixin, forms.ModelForm):
    weekdays = forms.MultipleChoiceField(choices=WEEKDAY_CHOICES, required=False,
                                         widget=forms.CheckboxSelectMultiple, label="Weekdays (weekly only)")

    class Meta:
        model = RecurringTask
        fields = ["title", "category", "priority", "assigned_to", "department", "location",
                  "frequency", "interval", "weekdays", "day_of_month", "skip_weekends",
                  "start_date", "end_date", "scheduled_start", "scheduled_end", "estimated_minutes",
                  "description", "expected_outcome", "is_active"]
        widgets = {"start_date": DATE, "end_date": DATE, "scheduled_start": TIME, "scheduled_end": TIME,
                   "description": TEXT3, "expected_outcome": forms.Textarea(attrs={"rows": 2})}
        labels = {"estimated_minutes": "Estimated duration (minutes)", "interval": "Repeat every"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_relations()
        self.fields["weekdays"].widget.attrs["class"] = "form-check-input"
        if self.instance.pk:
            self.initial["weekdays"] = [str(d) for d in self.instance.weekday_list]
        else:
            self.fields["start_date"].initial = timezone.localdate()

    def clean_weekdays(self):
        return ",".join(sorted(self.cleaned_data.get("weekdays") or []))


class CategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = TaskCategory
        fields = ["name", "activity_type", "description", "is_active"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if TaskCategory.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A category with this name already exists.")
        return name


def parse_local_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


class TaskAttachmentForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    files = MultipleFileField()
    description = forms.CharField(max_length=255, required=False, label="Description (optional)")
    files_label = "Evidence files"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()


class LinkTicketForm(BootstrapFormMixin, forms.Form):
    reference = forms.CharField(max_length=20, label="Ticket reference", help_text="e.g. INC-2026-00012")

    def clean_reference(self):
        from apps.helpdesk.models import Ticket

        ref = self.cleaned_data["reference"].strip().upper()
        self.ticket = Ticket.objects.filter(reference=ref).first()
        if self.ticket is None:
            raise forms.ValidationError("No ticket with this reference.")
        return ref
