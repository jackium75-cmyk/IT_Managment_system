"""
Background jobs. They run from ``python manage.py run_jobs`` (schedule it with
Windows Task Scheduler every 10 minutes) and, as a safety net, at most every
five minutes when IT staff open busy pages.
"""
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


def run_all():
    from apps.helpdesk.services import run_scheduled_jobs as helpdesk_jobs
    from apps.maintenance.services import run_jobs as maintenance_jobs
    from apps.tasks.recurrence import generate_all

    result = {"recurring_tasks": generate_all()}
    result["maintenance"] = maintenance_jobs()
    result.update(helpdesk_jobs())
    result.update(housekeeping())
    return result


LOGIN_ATTEMPT_RETENTION_DAYS = 90


def housekeeping():
    """Remove expired sign-in sessions and old lockout counters.

    Failed sign-ins are also written to the append-only audit log, which is never pruned;
    the LoginAttempt rows only feed the lockout check, which looks back minutes, not months.
    """
    from datetime import timedelta

    from django.contrib.sessions.models import Session
    from django.utils import timezone

    from apps.accounts.models import LoginAttempt

    now = timezone.now()
    sessions, _ = Session.objects.filter(expire_date__lt=now).delete()
    attempts, _ = LoginAttempt.objects.filter(timestamp__lt=now - timedelta(days=LOGIN_ATTEMPT_RETENTION_DAYS)).delete()
    return {"expired_sessions_removed": sessions, "old_login_attempts_removed": attempts}


def ensure_jobs_ran():
    if cache.add("core:jobs-ran", True, 300):
        try:
            run_all()
        except Exception:  # a background job must never break a page
            logger.exception("Scheduled jobs failed")
