from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """Send users with a temporary password to the change-password page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            profile = getattr(user, "profile", None)
            if profile is not None and profile.must_change_password:
                allowed = {reverse("accounts:password_change"), reverse("accounts:logout")}
                if request.path not in allowed and not request.path.startswith("/static/"):
                    return redirect("accounts:password_change")
        return self.get_response(request)
