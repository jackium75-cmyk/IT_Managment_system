from django import forms
from django.db.models import Q

from apps.accounts.models import Department
from apps.assets.models import Asset, Supplier
from apps.core.forms import BootstrapFormMixin
from apps.evidence.forms import EvidenceMixin, MultipleFileField
from apps.tasks.forms import DATE

from .models import Approval, ProcurementAttachment, ProcurementRequest

TA = forms.Textarea


class RequestForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ProcurementRequest
        fields = ["request_type", "item", "quantity", "reason", "department", "asset", "ticket", "project", "supplier",
                  "estimated_cost", "currency"]
        widgets = {"reason": TA(attrs={"rows": 3}), "ticket": forms.NumberInput}
        labels = {"ticket": "Related ticket (ID)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.helpdesk.models import Ticket
        from apps.projects.models import Project

        inst = self.instance if self.instance.pk else None
        self.fields["department"].queryset = Department.objects.filter(Q(is_active=True) | Q(pk=getattr(inst, "department_id", None)))
        self.fields["asset"].queryset = Asset.objects.filter(Q(pk__in=Asset.objects.in_service()) | Q(pk=getattr(inst, "asset_id", None)))
        self.fields["supplier"].queryset = Supplier.objects.filter(Q(is_active=True) | Q(pk=getattr(inst, "supplier_id", None)))
        self.fields["project"].queryset = Project.objects.exclude(status__in=["completed", "cancelled"]) | \
            Project.objects.filter(pk=getattr(inst, "project_id", None))
        self.fields["ticket"] = forms.ModelChoiceField(queryset=Ticket.objects.all(), required=False,
                                                       label="Related ticket", widget=forms.Select)
        self.fields["ticket"].queryset = Ticket.objects.filter(Q(pk__in=Ticket.objects.open()) | Q(pk=getattr(inst, "ticket_id", None)))
        self.fields["ticket"].widget.attrs["class"] = "form-select"
        self.fields["currency"].required = False
        self.fields["currency"].help_text = "Defaults to the hotel currency."
        self.order_fields(RequestForm.Meta.fields)

    def clean_currency(self):
        value = (self.cleaned_data.get("currency") or "").strip().upper()
        if value and not (len(value) == 3 and value.isalpha()):
            raise forms.ValidationError("Use a three-letter currency code.")
        return value


class TrackingForm(BootstrapFormMixin, forms.ModelForm):
    """Fields that may change after submission (supplier, dates, notes)."""

    class Meta:
        model = ProcurementRequest
        fields = ["supplier", "purchase_date", "warranty", "asset", "project"]
        widgets = {"purchase_date": DATE}


class DecisionForm(BootstrapFormMixin, forms.Form):
    decision = forms.ChoiceField(choices=Approval.Decision.choices, widget=forms.RadioSelect)
    approved_amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False,
                                         help_text="Leave empty to approve the estimated cost.")
    comment = forms.CharField(widget=TA(attrs={"rows": 2}), required=False, help_text="Required when rejecting.")


class DeliveredForm(BootstrapFormMixin, forms.Form):
    actual_expenditure = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                            help_text="What was actually paid (0 if free or under warranty).")
    delivery_date = forms.DateField(widget=DATE)
    warranty = forms.CharField(max_length=200, required=False)
    outcome = forms.CharField(widget=TA(attrs={"rows": 3}), required=False, label="Outcome")
    cost_saving_note = forms.CharField(widget=TA(attrs={"rows": 2}), required=False, label="Documented saving (optional)",
                                       help_text="Only if you can evidence it, e.g. 'Repaired for 80 instead of replacing for 650 "
                                                 "(quotation attached)'.")


class OrderedForm(BootstrapFormMixin, forms.Form):
    purchase_date = forms.DateField(widget=DATE, required=False)
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.filter(is_active=True), required=False)


class ReasonForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(widget=TA(attrs={"rows": 2}))


class AttachmentForm(EvidenceMixin, BootstrapFormMixin, forms.Form):
    files = MultipleFileField()
    doc_type = forms.ChoiceField(choices=ProcurementAttachment.DocType.choices, label="Document type")
    description = forms.CharField(max_length=255, required=False)
    files_label = "Quotations, invoices, delivery notes"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_files()
