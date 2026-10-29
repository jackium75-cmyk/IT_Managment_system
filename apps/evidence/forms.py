from django import forms
from django.core.exceptions import ValidationError

from apps.core.models import SystemSetting

from .validation import KNOWN_TYPES, MAX_FILES_PER_UPLOAD, validate_many


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Accepts several files; real validation happens in evidence.validation."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if not data:
            return []
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super(MultipleFileField, self).clean(f, initial) for f in files]


def accept_attr():
    exts = sorted(set(SystemSetting.get().allowed_extensions_list) & set(KNOWN_TYPES))
    return ",".join(f".{e}" for e in exts) + ",image/*,video/*"


class EvidenceMixin:
    """Adds a validated multi-file ``files`` field; validated uploads end up in ``self.uploads``."""

    files_label = "Photos, videos or documents"

    def setup_files(self):
        cfg = SystemSetting.get()
        field = self.fields["files"]
        field.label = self.files_label
        field.widget.attrs.update({
            "accept": accept_attr(), "multiple": True, "data-max-mb": cfg.max_upload_size_mb,
            "data-max-files": MAX_FILES_PER_UPLOAD, "class": "form-control evidence-input",
        })
        field.help_text = (f"Up to {MAX_FILES_PER_UPLOAD} files, {cfg.max_upload_size_mb} MB each. "
                           "On a phone you can take a photo or video directly.")
        self.uploads = []

    def clean_files(self):
        files = self.cleaned_data.get("files") or []
        if files:
            try:
                self.uploads = validate_many(files, SystemSetting.get())
            except ValidationError as exc:
                raise forms.ValidationError(exc.messages)
        return files
