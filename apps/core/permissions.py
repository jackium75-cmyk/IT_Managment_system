"""
Server-side permission enforcement.

Every protected view must use ``role_required`` / ``RoleRequiredMixin`` (or an
explicit object-level check). Hiding a button in a template is never treated
as access control.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied

from apps.accounts.roles import has_role

from . import audit


def _deny(request, reason):
    from .models import AuditLog

    audit.log(AuditLog.Action.ACCESS_DENIED, request=request, message=f"{reason}: {request.path}"[:500])
    raise PermissionDenied(reason)


def role_required(*roles):
    """Decorator for function views: user must be signed in and hold one of ``roles``."""

    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, *roles):
                _deny(request, "Your role does not permit this action")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


class RoleRequiredMixin(LoginRequiredMixin):
    """Mixin for class-based views. Set ``allowed_roles`` on the view."""

    allowed_roles = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not has_role(request.user, *self.allowed_roles):
            _deny(request, "Your role does not permit this action")
        return super().dispatch(request, *args, **kwargs)
