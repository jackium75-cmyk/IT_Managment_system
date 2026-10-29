from django import forms

from apps.assets.models import AssetCategory
from apps.core.forms import BootstrapFormMixin
from apps.helpdesk.models import TicketCategory

from .models import KnowledgeArticle

TA = forms.Textarea


class ArticleForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = KnowledgeArticle
        fields = ["title", "status", "category", "asset_category", "error_codes", "keywords", "problem", "symptoms",
                  "possible_causes", "diagnostics", "commands", "parts_replaced", "root_cause", "solution",
                  "verification", "prevention"]
        widgets = {f: TA(attrs={"rows": 3}) for f in ["problem", "symptoms", "possible_causes", "diagnostics",
                                                      "parts_replaced", "root_cause", "solution", "verification",
                                                      "prevention"]}
        widgets["commands"] = TA(attrs={"rows": 3, "class": "font-monospace"})
        widgets["error_codes"] = TA(attrs={"rows": 2})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = TicketCategory.objects.filter(parent__isnull=True)
        self.fields["asset_category"].queryset = AssetCategory.objects.filter(is_active=True)
