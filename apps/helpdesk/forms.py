from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.models import Location
from apps.core.forms import BootstrapFormMixin
from apps.evidence.forms import EvidenceMixin, MultipleFileField
from apps.tasks.forms import DATETIME, it_staff_queryset
from apps.tasks.models import Priority

from .models import EscalationRule, SLATarget, Ticket, TicketAttachment, TicketCategory, TicketComment


class TicketCreateForm(EvidenceMixin, BootstrapFormMixin, forms.ModelForm):
    """Deliberately simple: hotel staff should be able to report a problem in under a minute."""

    files = MultipleFileField()

    class Meta:
        model = Ticket
        fields = ["ticket_type", "subject", "category", "subcategory", "description", "impact", "urgency",
                  "location", "location_detail", "affected_device", "contact_phone"]
        labels = {"ticket_type": "What kind of request is this?", "subject": "Short summary",
                  "description": "What is happening? (what you see, any error message, when it started)",
                  "impact": "Who is affected?", "urgency": "How much does it stop work?",
                  "location": "Where?", "contact_phone": "Phone / extension to reach you"}
        widgets = {"description": forms.Textarea(attrs={"rows": 4}), "ticket_type": forms.RadioSelect,
                   "impact": forms.RadioSelect, "urgency": forms.RadioSelect}

    def __init__(self, *args, user=None, it_mode=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = TicketCategory.objects.filter(parent__isnull=True, is_active=True)
        self.fields["category"].empty_label = "Choose…"
        self.fields["subcategory"].queryset = TicketCategory.objects.filter(parent__isnull=False, is_active=True) \
            .select_related("parent")
        self.fields["subcategory"].label_from_instance = lambda c: c.name
        self.fields["subcategory"].widget.attrs["data-subcategory"] = "1"
        self.fields["location"].queryset = Location.objects.active()
        self.setup_files()
        if user is not None and not self.is_bound:
            self.initial.setdefault("location", user.profile.location_id)
            self.initial.setdefault("contact_phone", user.profile.phone)
        self.it_mode = it_mode
        if it_mode:
            self.fields["requester"] = forms.ModelChoiceField(
                queryset=get_user_model().objects.filter(is_active=True).order_by("first_name", "last_name"),
                label="Requester (person reporting the problem)")
            self.fields["requester"].label_from_instance = lambda u: f"{u.get_full_name() or u.username} ({u.profile.department or 'no department'})"
            self.fields["requester"].widget.attrs["class"] = "form-select"
            self.order_fields(["requester"] + list(self.Meta.fields) + ["files"])

    def subcategory_map(self):
        """{category id: [(sub id, name), ...]} for the dependent drop-down."""
        out = {}
        for sub in self.fields["subcategory"].queryset:
            out.setdefault(str(sub.parent_id), []).append([sub.pk, sub.name])
        return out


class CommentForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False, label="Message")
    kind = forms.ChoiceField(choices=TicketComment.Kind.choices[:3], initial=TicketComment.Kind.PUBLIC, label="Type")
    files = MultipleFileField()
    stage = forms.ChoiceField(choices=TicketAttachment.Stage.choices, required=False, label="Evidence stage",
                              initial=TicketAttachment.Stage.TROUBLESHOOTING)
    internal_files = forms.BooleanField(required=False, label="Files visible to IT only")
    files_label = "Attach evidence"

    def __init__(self, *args, it=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()
        if not it:
            for name in ("kind", "stage", "internal_files"):
                del self.fields[name]


class StatusForm(BootstrapFormMixin, forms.Form):
    status = forms.ChoiceField(choices=Ticket.Status.choices, label="New status")
    waiting_on = forms.ChoiceField(choices=[("", "—")] + list(Ticket.WaitingOn.choices), required=False,
                                   label="Waiting for")
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False,
                           label="Note / question for the requester")
    resolution_details = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False,
                                         label="Resolution (what fixed it)")
    root_cause = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                                 label="Root cause (if known)")
    verification = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                                   label="Verification / tests done")


class AssignForm(BootstrapFormMixin, forms.Form):
    assigned_to = forms.ModelChoiceField(queryset=None, required=False, label="Assign to", empty_label="Unassigned")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = it_staff_queryset()
        self.fields["assigned_to"].label_from_instance = lambda u: u.get_full_name() or u.username


class PriorityForm(BootstrapFormMixin, forms.Form):
    priority = forms.ChoiceField(choices=Priority.choices)
    reason = forms.CharField(max_length=300)


class ConfirmForm(BootstrapFormMixin, forms.Form):
    rating = forms.TypedChoiceField(
        choices=[("", "No rating"), (5, "5 – Excellent"), (4, "4 – Good"), (3, "3 – OK"), (2, "2 – Poor"),
                 (1, "1 – Very poor")], coerce=int, empty_value=None, required=False, label="How was the service?")
    comment = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False, label="Comments (optional)")


class ReopenForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="What is still wrong?")


class HideAttachmentForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, label="Reason for removing this file from view")


class LogTimeForm(BootstrapFormMixin, forms.Form):
    started_at = forms.DateTimeField(widget=DATETIME, label="Started", input_formats=["%Y-%m-%dT%H:%M"])
    minutes = forms.IntegerField(min_value=1, max_value=24 * 60, label="Duration (minutes)")
    note = forms.CharField(max_length=300, required=False)

    def clean_started_at(self):
        value = self.cleaned_data["started_at"]
        return timezone.make_aware(value) if timezone.is_naive(value) else value


class VoidForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(max_length=300, label="Reason for voiding")


class TicketCategoryForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = TicketCategory
        fields = ["name", "parent", "description", "sort_order", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = TicketCategory.objects.filter(parent__isnull=True).exclude(pk=self.instance.pk)
        self.fields["parent"].label = "Parent category (leave empty for a top-level category)"

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and cleaned.get("parent") and self.instance.children.exists():
            raise forms.ValidationError("A category that has subcategories cannot itself become a subcategory.")
        return cleaned


class SLATargetForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SLATarget
        fields = ["response_minutes", "resolution_minutes"]


class EscalationRuleForm(BootstrapFormMixin, forms.ModelForm):
    priorities = forms.MultipleChoiceField(choices=Priority.choices, required=False, widget=forms.CheckboxSelectMultiple,
                                           help_text="None ticked means every priority.")

    class Meta:
        model = EscalationRule
        fields = ["name", "trigger", "threshold", "priorities", "notify_assignee", "notify_it_staff",
                  "notify_admins", "notify_department_head", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["priorities"] = self.instance.priority_list

    def clean_priorities(self):
        return ",".join(self.cleaned_data.get("priorities") or [])

    def clean(self):
        cleaned = super().clean()
        trigger, threshold = cleaned.get("trigger"), cleaned.get("threshold") or 0
        if trigger == EscalationRule.Trigger.RESOLUTION_WARNING and not 1 <= threshold <= 99:
            self.add_error("threshold", "Enter a percentage between 1 and 99.")
        if trigger == EscalationRule.Trigger.UNASSIGNED and threshold < 1:
            self.add_error("threshold", "Enter the number of minutes.")
        if not any(cleaned.get(f) for f in ("notify_assignee", "notify_it_staff", "notify_admins", "notify_department_head")):
            raise forms.ValidationError("Choose at least one person to notify.")
        return cleaned
