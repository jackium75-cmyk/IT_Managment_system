"""Backup records, locking, rotation, copying to a second location, and audit entries."""
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connections
from django.urls import reverse
from django.utils import timezone

from apps.core import audit
from apps.core.models import AuditLog

from . import engine
from .models import BackupRecord

logger = logging.getLogger(__name__)
R = BackupRecord
LOCK_NAME = ".backup.lock"
STALE_AFTER = timedelta(hours=6)
OVERDUE_AFTER = timedelta(hours=26)  # a nightly backup plus some slack


class BackupBusy(engine.BackupError):
    pass


def backup_root():
    return Path(settings.BACKUP_ROOT)


# ---------------------------------------------------------------------------
# Locking: one backup or restore at a time, across processes
# ---------------------------------------------------------------------------
class _Lock:
    def __init__(self):
        self.path = backup_root() / LOCK_NAME

    def __enter__(self):
        backup_root().mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - self.path.stat().st_mtime > STALE_AFTER.total_seconds():
                self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BackupBusy("Another backup, verification or restore is already running. Try again later.")
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def is_busy():
    lock = backup_root() / LOCK_NAME
    return lock.exists() and time.time() - lock.stat().st_mtime < STALE_AFTER.total_seconds()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def _notify_failure(title, body):
    from apps.accounts.roles import Role
    from apps.notifications.models import Notification
    from apps.notifications.services import notify
    from django.contrib.auth import get_user_model

    admins = get_user_model().objects.filter(is_active=True, profile__role=Role.ADMIN)
    notify(admins, Notification.Kind.SYSTEM, title, body=body[:1000], url=reverse("backups:list"),
           dedupe_key="backup-failure", window_minutes=12 * 60)


def run_backup(kind=R.Kind.MANUAL, user=None, *, verify=False):
    """Make a backup, copy it to the second location, rotate old ones. Returns the BackupRecord."""
    expire_stale_runs()
    lock = _Lock().__enter__()  # raises BackupBusy before anything is recorded
    try:
        record = R.objects.create(kind=kind, triggered_by=user, started_at=timezone.now(), db_engine=_vendor())
        try:
            info = engine.create_archive(backup_root(), record_id=record.pk)
            record.file_name = info["path"].name
            record.size_bytes = info["size"]
            record.sha256 = info["sha256"]
            record.media_files = len(info["manifest"]["files"])
            record.status = R.Status.SUCCESS
            record.message = (f"{engine.human_size(info['size'])}; database and {record.media_files} uploaded "
                              f"file(s).")
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded and reported
            logger.exception("Backup failed")
            record.status = R.Status.FAILED
            record.message = str(exc) if isinstance(exc, engine.BackupError) else f"Unexpected error: {exc}"
    finally:
        lock.__exit__()
    record.finished_at = timezone.now()
    record.save()
    audit.log(AuditLog.Action.BACKUP, record, actor=user,
              message=f"Backup {record.get_status_display().lower()}: {record.message}"[:500])
    if record.status == R.Status.FAILED:
        _notify_failure("Backup failed", record.message)
        return record
    copy_to_second_location(record)
    if verify:
        run_verify(record, user)
    rotate()
    return record


def _vendor():
    return connections["default"].vendor


def expire_stale_runs():
    """A backup or verification holds the lock while it runs; without the lock, "running" is left over from a
    process that was stopped (server restart, power cut)."""
    cutoff = timezone.now() - (STALE_AFTER if is_busy() else timedelta(0))
    R.objects.filter(status=R.Status.RUNNING, started_at__lt=cutoff).update(
        status=R.Status.FAILED, finished_at=timezone.now(), message="Did not finish (process stopped).")
    R.objects.filter(verify_status=R.Verify.RUNNING, verified_at__lt=cutoff).update(
        verify_status=R.Verify.FAILED, verify_message="Verification did not finish (process stopped).")


def copy_to_second_location(record):
    target = settings.BACKUP_COPY_TO
    if not target or not record.file_name:
        return
    src = backup_root() / record.file_name
    try:
        dest_dir = Path(target)
        dest_dir.mkdir(parents=True, exist_ok=True)
        partial = dest_dir / f"{record.file_name}.partial"
        shutil.copyfile(src, partial)
        if engine.sha256_file(partial) != record.sha256:
            partial.unlink(missing_ok=True)
            raise engine.BackupError("the copied file's fingerprint does not match the original")
        os.replace(partial, dest_dir / record.file_name)
        shutil.copyfile(engine.sidecar_path(src), dest_dir / engine.sidecar_path(src).name)
        record.copied_to, record.copy_error = str(dest_dir), ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("Copying backup to %s failed", target)
        record.copied_to, record.copy_error = "", f"Copy to {target} failed: {exc}"
        _notify_failure("Backup copy to second location failed", record.copy_error)
    record.save(update_fields=["copied_to", "copy_error"])


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
def run_verify(record, user=None):
    record.verify_status, record.verified_at, record.verify_message = R.Verify.RUNNING, timezone.now(), ""
    record.save(update_fields=["verify_status", "verified_at", "verify_message"])
    try:
        with _Lock():
            result = engine.verify_archive(backup_root() / record.file_name)
        record.verify_status = R.Verify.OK if result.ok else R.Verify.FAILED
        record.verify_message = result.summary
    except BackupBusy as exc:
        record.verify_status, record.verify_message = R.Verify.NOT_VERIFIED, str(exc)
        record.save(update_fields=["verify_status", "verify_message"])
        return record
    except engine.BackupError as exc:
        record.verify_status, record.verify_message = R.Verify.FAILED, str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Verification failed")
        record.verify_status, record.verify_message = R.Verify.FAILED, f"Unexpected error: {exc}"
    record.verified_at = timezone.now()
    record.save(update_fields=["verify_status", "verified_at", "verify_message"])
    audit.log(AuditLog.Action.BACKUP, record, actor=user,
              message=f"Backup verification: {record.get_verify_status_display()}")
    if record.verify_status == R.Verify.FAILED:
        _notify_failure(f"Backup {record.file_name} failed verification", record.verify_message)
    return record


# ---------------------------------------------------------------------------
# Rotation and the backup folder
# ---------------------------------------------------------------------------
def rotate(now=None):
    """Keep the newest BACKUP_KEEP_DAILY backups, the newest of each of the last BACKUP_KEEP_MONTHLY months,
    and always the newest verified backup. Returns the records removed."""
    successful = list(R.objects.filter(status=R.Status.SUCCESS).exclude(file_name="").order_by("-started_at"))
    keep = {r.pk for r in successful[:max(settings.BACKUP_KEEP_DAILY, 1)]}
    months = {}
    for r in successful:
        key = timezone.localtime(r.started_at).strftime("%Y-%m")
        if key not in months and len(months) < settings.BACKUP_KEEP_MONTHLY:
            months[key] = r.pk
    keep |= set(months.values())
    newest_verified = next((r for r in successful if r.verify_status == R.Verify.OK), None)
    if newest_verified:
        keep.add(newest_verified.pk)
    removed = []
    for r in successful:
        if r.pk in keep:
            continue
        for folder in filter(None, [backup_root(), Path(settings.BACKUP_COPY_TO) if settings.BACKUP_COPY_TO else None]):
            for p in (folder / r.file_name, engine.sidecar_path(folder / r.file_name)):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not delete old backup %s", p)
        r.status, r.rotated_at = R.Status.ROTATED, now or timezone.now()
        r.message = (r.message + "\nRemoved by rotation.").strip()
        r.save(update_fields=["status", "rotated_at", "message"])
        removed.append(r)
    if removed:
        audit.log(AuditLog.Action.BACKUP, message=f"Rotation removed {len(removed)} old backup(s): "
                                                  + ", ".join(r.file_name for r in removed)[:400])
    return removed


def sync_records():
    """Match records with the files actually in BACKUP_ROOT (files copied in, or deleted by hand)."""
    root = backup_root()
    if not root.exists():
        return
    known = set(R.objects.exclude(file_name="").values_list("file_name", flat=True))
    for path in sorted(root.glob(f"{engine.PREFIX}*.zip")):
        if path.name in known:
            continue
        sidecar = engine.sidecar_path(path)
        sha = sidecar.read_text(encoding="ascii", errors="replace").split()[0] if sidecar.exists() else ""
        stat = path.stat()
        R.objects.create(kind=R.Kind.FOUND, status=R.Status.SUCCESS, file_name=path.name, size_bytes=stat.st_size,
                         sha256=sha[:64], started_at=datetime.fromtimestamp(stat.st_mtime, tz=dt_timezone.utc),
                         finished_at=timezone.now(), message="Found in the backup folder; not made by this database.")
    for r in R.objects.filter(status=R.Status.SUCCESS).exclude(file_name=""):
        if not (root / r.file_name).exists():
            r.status, r.rotated_at = R.Status.ROTATED, timezone.now()
            r.message = (r.message + "\nThe file is no longer in the backup folder.").strip()
            r.save(update_fields=["status", "rotated_at", "message"])


def after_restore(path, manifest):
    """Tidy the records that came back with the restored database."""
    path = Path(path)
    R.objects.filter(pk=manifest.get("record_id"), status=R.Status.RUNNING).update(
        status=R.Status.SUCCESS, file_name=path.name, size_bytes=path.stat().st_size,
        sha256=engine.sha256_file(path), media_files=len(manifest.get("files", [])), finished_at=timezone.now(),
        message="This is the backup that was restored.")
    R.objects.filter(status=R.Status.RUNNING).update(
        status=R.Status.FAILED, finished_at=timezone.now(), message="Was running when the restored backup was taken.")
    R.objects.filter(verify_status=R.Verify.RUNNING).update(verify_status=R.Verify.NOT_VERIFIED)
    sync_records()


def last_success():
    return R.objects.filter(status=R.Status.SUCCESS).order_by("-started_at").first()


def health():
    """Facts for the backup page, the dashboard and the preflight check."""
    last = last_success()
    root = backup_root()
    usage = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(root)
    except OSError:
        pass
    overdue = last is None or timezone.now() - last.started_at > OVERDUE_AFTER
    return {"last": last, "overdue": overdue, "root": root, "copy_to": settings.BACKUP_COPY_TO,
            "free": usage.free if usage else None, "total": usage.total if usage else None,
            "busy": is_busy(), "keep_daily": settings.BACKUP_KEEP_DAILY, "keep_monthly": settings.BACKUP_KEEP_MONTHLY,
            "last_verified": R.objects.filter(status=R.Status.SUCCESS, verify_status=R.Verify.OK)
                             .order_by("-started_at").first()}


# ---------------------------------------------------------------------------
# Background execution for the web page
# ---------------------------------------------------------------------------
def in_background(func, *args, **kwargs):
    if not settings.BACKUP_RUN_IN_THREAD:
        return func(*args, **kwargs)

    def runner():
        close_old_connections()
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("Background backup task failed")
        finally:
            connections.close_all()

    threading.Thread(target=runner, name="hotel-it-backup", daemon=False).start()
    return None
