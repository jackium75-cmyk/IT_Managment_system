"""
Upload validation for evidence files (photos, videos, documents, logs).

Defence in depth:
* the extension must be on the administrator's allow-list AND be a type this
  module knows how to verify; script/executable types can never be enabled;
* the file's real content (magic bytes / structure) must match its extension;
* images are opened and verified by Pillow (with a decompression-bomb limit);
* Office files must be genuine OOXML without macros;
* text/log/CSV files must be text, not binary;
* size limits come from System Configuration.
Files are later stored under a random name, never the user's file name.
"""
import os
import zipfile

from django.core.exceptions import ValidationError
from PIL import Image

IMAGE = "image"
VIDEO = "video"
DOCUMENT = "document"

# extension -> (kind, content type used when serving)
KNOWN_TYPES = {
    "jpg": (IMAGE, "image/jpeg"), "jpeg": (IMAGE, "image/jpeg"), "png": (IMAGE, "image/png"),
    "gif": (IMAGE, "image/gif"), "webp": (IMAGE, "image/webp"), "heic": (IMAGE, "image/heic"),
    "mp4": (VIDEO, "video/mp4"), "m4v": (VIDEO, "video/mp4"), "mov": (VIDEO, "video/quicktime"),
    "webm": (VIDEO, "video/webm"), "3gp": (VIDEO, "video/3gpp"),
    "pdf": (DOCUMENT, "application/pdf"),
    "docx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "txt": (DOCUMENT, "text/plain"), "log": (DOCUMENT, "text/plain"), "csv": (DOCUMENT, "text/csv"),
}

# Never accepted, whatever an administrator configures.
BLOCKED_EXTENSIONS = {
    "exe", "bat", "cmd", "com", "msi", "ps1", "vbs", "js", "jar", "sh", "php", "py", "html", "htm", "svg",
    "dll", "scr", "hta", "asp", "aspx", "jsp", "cgi", "pl", "docm", "xlsm", "lnk", "reg", "iso",
}

MAX_FILES_PER_UPLOAD = 10
Image.MAX_IMAGE_PIXELS = 80_000_000  # ~80 MP; larger "images" are rejected as decompression bombs


def extension_of(name):
    base = os.path.basename(name or "")
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _iso_media(head):
    return len(head) >= 12 and head[4:8] == b"ftyp"


def _check_signature(ext, head):
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == "heic":
        return _iso_media(head) and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim")
    if ext in ("mp4", "m4v", "mov", "3gp"):
        return _iso_media(head) or (ext == "mov" and head[4:8] in (b"moov", b"mdat", b"wide", b"free"))
    if ext == "webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext == "pdf":
        return head.lstrip()[:5] == b"%PDF-"
    if ext in ("docx", "xlsx"):
        return head.startswith(b"PK\x03\x04")
    if ext in ("txt", "log", "csv"):
        return True
    return False


def _check_text(f):
    sample = f.read(64 * 1024)
    if b"\x00" in sample:
        raise ValidationError("This does not look like a text file.")
    for enc in ("utf-8", "cp1252"):
        try:
            sample.decode(enc)
            return
        except UnicodeDecodeError:
            continue
    raise ValidationError("This text file uses an unsupported encoding.")


def _check_ooxml(f, ext):
    try:
        with zipfile.ZipFile(f) as z:
            names = z.namelist()
            if len(names) > 5000:
                raise ValidationError("This document has an unusual structure and was rejected.")
            total = sum(i.file_size for i in z.infolist())
            if total > 500 * 1024 * 1024:
                raise ValidationError("This document expands to an unsafe size and was rejected.")
    except zipfile.BadZipFile:
        raise ValidationError("This Office document is damaged or not a real .%s file." % ext)
    if "[Content_Types].xml" not in names:
        raise ValidationError("This is not a genuine .%s file." % ext)
    prefix = "word/" if ext == "docx" else "xl/"
    if not any(n.startswith(prefix) for n in names):
        raise ValidationError("The file content does not match the .%s extension." % ext)
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise ValidationError("Documents containing macros are not accepted.")


def _check_image(f, ext):
    if ext == "heic":
        return  # Pillow cannot decode HEIC without plugins; signature check above applies.
    try:
        with Image.open(f) as img:
            img.verify()
    except Image.DecompressionBombError:
        raise ValidationError("This image is too large in dimensions to be accepted.")
    except Exception:
        raise ValidationError("This image is damaged or not a real .%s file." % ext)


def validate_upload(f, settings_obj):
    """
    Validate one uploaded file. Returns (extension, kind, content_type).
    Raises ValidationError with a message suitable for the user.
    """
    name = os.path.basename(getattr(f, "name", "") or "")
    ext = extension_of(name)
    allowed = set(settings_obj.allowed_extensions_list) - BLOCKED_EXTENSIONS
    if not ext or ext in BLOCKED_EXTENSIONS or ext not in allowed or ext not in KNOWN_TYPES:
        raise ValidationError(f"“{name}”: files of type .{ext or '?'} are not accepted. "
                              f"Allowed: {', '.join(sorted(allowed & set(KNOWN_TYPES)))}.")
    limit = settings_obj.max_upload_size_mb * 1024 * 1024
    if f.size == 0:
        raise ValidationError(f"“{name}” is empty.")
    if f.size > limit:
        raise ValidationError(f"“{name}” is {f.size / 1048576:.1f} MB; the limit is {settings_obj.max_upload_size_mb} MB.")
    f.seek(0)
    head = f.read(64)
    f.seek(0)
    if not _check_signature(ext, head):
        raise ValidationError(f"“{name}”: the file content does not match a .{ext} file.")
    try:
        kind, content_type = KNOWN_TYPES[ext]
        if ext in ("txt", "log", "csv"):
            _check_text(f)
        elif ext in ("docx", "xlsx"):
            _check_ooxml(f, ext)
        elif kind == IMAGE:
            _check_image(f, ext)
    except ValidationError as exc:
        raise ValidationError(f"“{name}”: {exc.messages[0]}")
    finally:
        f.seek(0)
    return ext, kind, content_type


def validate_many(files, settings_obj):
    """Validate a list of uploads; collects every error so the user sees them all at once."""
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise ValidationError(f"Attach at most {MAX_FILES_PER_UPLOAD} files at a time.")
    results, errors = [], []
    for f in files:
        try:
            results.append((f, *validate_upload(f, settings_obj)))
        except ValidationError as exc:
            errors.extend(exc.messages)
    if errors:
        raise ValidationError(errors)
    return results
