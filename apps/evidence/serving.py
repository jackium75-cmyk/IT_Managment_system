"""
Streaming of evidence files AFTER the caller has checked permissions.

Supports HTTP Range requests so videos can be played and seeked (required by
Safari / iPhone). Files are always sent with nosniff and a restrictive CSP so a
crafted file cannot run script in the application's origin.
"""
import os
import re

from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header

INLINE_KINDS = {"image", "video"}
INLINE_TYPES = {"application/pdf"}
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")
CHUNK = 64 * 1024


def _security_headers(response, inline):
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    if inline:
        # PDFs need the browser's viewer; everything else gets a sandbox.
        response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
    else:
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def _iter_range(fh, start, length):
    try:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            data = fh.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        fh.close()


def serve(request, attachment, *, download=False):
    try:
        path = attachment.file.path
        size = os.path.getsize(path)
    except (ValueError, OSError):
        raise Http404("The file is missing from storage.")
    inline = not download and (attachment.kind in INLINE_KINDS or attachment.content_type in INLINE_TYPES)
    disposition = content_disposition_header(not inline, attachment.original_name)

    match = RANGE_RE.match(request.headers.get("Range", "").strip())
    if match and (match.group(1) or match.group(2)):
        first, last = match.group(1), match.group(2)
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(last)), size - 1
        if start >= size or start > end:
            resp = HttpResponse(status=416)
            resp["Content-Range"] = f"bytes */{size}"
            return _security_headers(resp, inline)
        length = end - start + 1
        resp = StreamingHttpResponse(_iter_range(open(path, "rb"), start, length), status=206,
                                     content_type=attachment.content_type)
        resp["Content-Length"] = str(length)
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
    else:
        resp = FileResponse(open(path, "rb"), content_type=attachment.content_type)
        resp["Content-Length"] = str(size)
    resp["Accept-Ranges"] = "bytes"
    resp["Content-Disposition"] = disposition
    return _security_headers(resp, inline)
