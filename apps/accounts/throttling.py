"""
Login brute-force protection.

A username is locked after ``login_max_failures`` consecutive failures within
``login_lockout_minutes``. An IP address is locked after four times that many
failures in the same window, which stops password spraying across accounts.
A successful sign-in resets the username's failure count.
"""
from datetime import timedelta

from django.utils import timezone

from apps.core.models import SystemSetting

from .models import LoginAttempt

IP_MULTIPLIER = 4


def _limits():
    cfg = SystemSetting.get()
    return cfg.login_max_failures, timedelta(minutes=cfg.login_lockout_minutes)


def normalise(username):
    return (username or "").strip().lower()[:150]


def is_locked(username, ip):
    max_failures, window = _limits()
    since = timezone.now() - window
    username = normalise(username)

    last_success = (
        LoginAttempt.objects.filter(username=username, successful=True, timestamp__gte=since)
        .order_by("-timestamp").values_list("timestamp", flat=True).first()
    )
    user_failures = LoginAttempt.objects.filter(username=username, successful=False, timestamp__gte=since)
    if last_success:
        user_failures = user_failures.filter(timestamp__gt=last_success)
    if user_failures.count() >= max_failures:
        return True
    if ip:
        ip_failures = LoginAttempt.objects.filter(ip_address=ip, successful=False, timestamp__gte=since).count()
        if ip_failures >= max_failures * IP_MULTIPLIER:
            return True
    return False


def record(username, ip, successful):
    LoginAttempt.objects.create(username=normalise(username), ip_address=ip, successful=successful)


def lockout_minutes():
    return SystemSetting.get().login_lockout_minutes
