import zoneinfo

from django.utils import timezone

from . import audit


class HotelTimezoneMiddleware:
    """Display all dates and times in the hotel's configured timezone."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .models import SystemSetting

        try:
            timezone.activate(zoneinfo.ZoneInfo(SystemSetting.get().hotel_timezone))
        except Exception:  # missing table during first migrate, bad value, etc.
            timezone.deactivate()
        return self.get_response(request)


class AuditRequestMiddleware:
    """Make the current request available to audit logging helpers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        audit.set_current_request(request)
        try:
            return self.get_response(request)
        finally:
            audit.set_current_request(None)


CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",                 # no inline or third-party scripts
    "style-src 'self' 'unsafe-inline'",  # style attributes only; cannot run code
    "img-src 'self' data:",              # data: for Bootstrap's built-in form icons
    "font-src 'self'",
    "media-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
PERMISSIONS_POLICY = "camera=(self), microphone=(), geolocation=(), payment=(), usb=()"


class SecurityHeadersMiddleware:
    """Site-wide Content-Security-Policy, Permissions-Policy and no-store caching.

    Pages for signed-in users are marked ``no-store`` so that pressing Back on a
    shared front-desk computer after sign-out does not show the previous user's
    data. Views that set their own headers (evidence downloads, QR images) keep them.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "private, no-store"
        return response
