"""
Helpers for writing audit log entries.

Use ``audit.log(...)`` from views and services. The current request (if any)
is picked up from ``AuditRequestMiddleware`` so the IP address and user agent
are recorded without passing the request everywhere.
"""
import logging
import threading

from django.contrib.contenttypes.models import ContentType
from django.db import models

logger = logging.getLogger(__name__)
_local = threading.local()


def set_current_request(request):
    _local.request = request


def get_current_request():
    return getattr(_local, "request", None)


def client_ip(request):
    if request is None:
        return None
    # REMOTE_ADDR is used deliberately. X-Forwarded-For is only trustworthy
    # behind a known reverse proxy and is easy to spoof otherwise.
    return request.META.get("REMOTE_ADDR") or None


def _jsonable(value):
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def diff_instances(old, new, fields):
    """Return {field: [old, new]} for fields whose values changed."""
    changes = {}
    for field in fields:
        before = getattr(old, field, None) if old is not None else None
        after = getattr(new, field, None)
        if before != after:
            changes[field] = [_jsonable(before), _jsonable(after)]
    return changes


def log(action, obj=None, *, actor=None, message="", changes=None, request=None, object_repr=None):
    from .models import AuditLog

    request = request or get_current_request()
    if actor is None and request is not None and getattr(request, "user", None) is not None:
        if request.user.is_authenticated:
            actor = request.user
    entry = AuditLog(
        action=action,
        actor=actor if (actor is not None and actor.pk) else None,
        actor_username=getattr(actor, "username", "") or "",
        message=message[:500],
        changes=changes or {},
        ip_address=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request is not None else ""),
    )
    if obj is not None:
        entry.content_type = ContentType.objects.get_for_model(obj, for_concrete_model=False)
        entry.object_id = str(obj.pk)
        entry.object_repr = (object_repr or str(obj))[:255]
    elif object_repr:
        entry.object_repr = object_repr[:255]
    entry.save()
    logger.info("AUDIT %s by %s on %s: %s", action, entry.actor_username or "system", entry.object_repr, message)
    return entry
