"""
Sending notifications.

* In-app notifications are always created.
* Email is sent when enabled in System Configuration, the recipient has an
  email address and has not opted out; it is sent after the database
  transaction commits so a rolled-back action never emails anyone.
* Duplicates are suppressed: the same ``dedupe_key`` is not sent to the same
  person again within ``window_minutes`` (or while an earlier one is unread).
* SMS / WhatsApp are not built in; they need an authorised provider and can be
  added as another channel in ``_send_email``'s place.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from apps.core.models import SystemSetting

from .models import Notification

logger = logging.getLogger(__name__)
DEFAULT_WINDOW_MINUTES = 60


def _recently_sent(user, key, window_minutes):
    if not key:
        return False
    since = timezone.now() - timedelta(minutes=window_minutes)
    return Notification.objects.filter(recipient=user, dedupe_key=key).filter(
        is_read=False).exists() or Notification.objects.filter(
        recipient=user, dedupe_key=key, created_at__gte=since).exists()


def _send_email(notification_id):
    n = Notification.objects.select_related("recipient").get(pk=notification_id)
    site = SystemSetting.get()
    link = n.url
    base = getattr(settings, "SITE_BASE_URL", "")
    if base and link.startswith("/"):
        link = base.rstrip("/") + link
    body = f"{n.body}\n\n{link}\n\n— {site.hotel_name} IT".strip()
    try:
        send_mail(f"[{site.hotel_name} IT] {n.title}", body, settings.DEFAULT_FROM_EMAIL, [n.recipient.email])
        Notification.objects.filter(pk=n.pk).update(emailed_at=timezone.now())
    except Exception as exc:  # network / SMTP problems must never break the application
        logger.warning("Email notification %s failed: %s", n.pk, exc)
        Notification.objects.filter(pk=n.pk).update(email_error=str(exc)[:300])


def notify(recipients, kind, title, *, body="", url="", dedupe_key="", window_minutes=DEFAULT_WINDOW_MINUTES,
           exclude=None):
    """Create notifications for each distinct active recipient. Returns the list created."""
    created = []
    seen = set()
    email_on = SystemSetting.get().email_notifications_enabled
    for user in recipients:
        if user is None or not user.is_active or user.pk in seen or (exclude is not None and user.pk == exclude.pk):
            continue
        seen.add(user.pk)
        if _recently_sent(user, dedupe_key, window_minutes):
            continue
        n = Notification.objects.create(recipient=user, kind=kind, title=title[:200], body=body, url=url,
                                        dedupe_key=dedupe_key[:120])
        created.append(n)
        profile = getattr(user, "profile", None)
        if email_on and user.email and (profile is None or profile.email_notifications):
            transaction.on_commit(lambda pk=n.pk: _send_email(pk))
    return created


def unread_count(user):
    return Notification.objects.filter(recipient=user, is_read=False).count()


def it_staff():
    from apps.tasks.forms import it_staff_queryset

    return it_staff_queryset()


def admins():
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from apps.accounts.roles import Role

    return get_user_model().objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(profile__role=Role.ADMIN))
