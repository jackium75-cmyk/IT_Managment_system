from apps.accounts.roles import Role, get_role

from .models import SystemSetting
from .navigation import sidebar_for


def site_context(request):
    ctx = {"ROLE": Role}
    try:
        ctx["site_settings"] = SystemSetting.get()
    except Exception:
        ctx["site_settings"] = None
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        ctx["user_role"] = get_role(user)
        ctx["sidebar"] = sidebar_for(user, request.path)
    return ctx
