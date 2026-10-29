"""Check that this installation is safe to run in production. Run after installing and after every update."""
import os
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

OK, WARN, FAIL = "OK", "WARN", "FAIL"


class Command(BaseCommand):
    help = "Production readiness check: settings, database, folders, backups, demo data and Django's deploy checks."

    def add_arguments(self, parser):
        parser.add_argument("--dev", action="store_true", help="Accept development settings (DEBUG, SQLite).")

    def handle(self, *args, **opts):
        self.dev = opts["dev"]
        self.results = []
        for check in (self.check_settings, self.check_database, self.check_folders, self.check_static,
                      self.check_backups, self.check_accounts, self.check_demo_data, self.check_email):
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - report every check, never stop half way
                self.add(FAIL, check.__name__.replace("check_", "").title(), f"Check could not run: {exc}")
        width = max(len(name) for _s, name, _m in self.results)
        style = {OK: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}
        for status, name, message in self.results:
            self.stdout.write(f"{style[status](f'[{status:<4}]')} {name:<{width}}  {message}")
        self.stdout.write("\nDjango deployment checks:")
        call_command("check", deploy=True, fail_level="ERROR")
        fails = sum(1 for s, *_ in self.results if s == FAIL)
        warns = sum(1 for s, *_ in self.results if s == WARN)
        if fails:
            raise CommandError(f"{fails} problem(s) must be fixed before going live ({warns} warning(s)).")
        self.stdout.write(self.style.SUCCESS(f"Ready. {warns} warning(s) to review."))

    def add(self, status, name, message):
        self.results.append((status, name, message))

    def strict(self):
        return WARN if self.dev else FAIL

    # ------------------------------------------------------------------
    def check_settings(self):
        if settings.DEBUG:
            self.add(self.strict(), "Debug mode", "DJANGO_DEBUG is on: detailed error pages would reveal internals. "
                                                  "Set DJANGO_DEBUG=0.")
        else:
            self.add(OK, "Debug mode", "Off.")
        key = settings.SECRET_KEY or ""
        if key.startswith("dev-only") or len(key) < 40:
            self.add(self.strict(), "Secret key", "DJANGO_SECRET_KEY is missing, short or the development key.")
        else:
            self.add(OK, "Secret key", "Set (not shown).")
        hosts = settings.ALLOWED_HOSTS
        if not hosts or "*" in hosts:
            self.add(FAIL, "Allowed hosts", "DJANGO_ALLOWED_HOSTS must list the server's names/addresses, not '*'.")
        else:
            self.add(OK, "Allowed hosts", ", ".join(hosts))
        if settings.USE_HTTPS:
            self.add(OK, "HTTPS", "Secure cookies and redirect to HTTPS are on.")
        else:
            self.add(WARN, "HTTPS", "Off. Passwords cross the network unencrypted; see docs/DEPLOYMENT.md (HTTPS).")
        if not settings.SITE_BASE_URL:
            self.add(WARN, "Site address", "SITE_BASE_URL is empty, so links in notification emails cannot be built.")

    def check_database(self):
        connection.ensure_connection()
        if connection.vendor == "postgresql":
            self.add(OK, "Database", f"PostgreSQL {connection.cursor().connection.info.server_version // 10000}, "
                                     f"database {connection.settings_dict['NAME']}.")
        else:
            self.add(self.strict(), "Database", "SQLite is for development and training only. Use PostgreSQL "
                                                "(DB_ENGINE=postgres) in production.")
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending:
            self.add(FAIL, "Migrations", f"{len(pending)} database change(s) not applied. Run: manage.py migrate")
        else:
            self.add(OK, "Migrations", "All applied.")

    def check_folders(self):
        base = Path(settings.BASE_DIR).resolve()
        media = Path(settings.MEDIA_ROOT).resolve()
        static_dirs = [Path(d).resolve() for d in settings.STATICFILES_DIRS] + [Path(settings.STATIC_ROOT).resolve()]
        if any(media == d or d in media.parents for d in static_dirs):
            self.add(FAIL, "Uploads folder", f"{media} is inside a static folder and could be served publicly.")
        elif self._writable(media):
            self.add(OK, "Uploads folder", f"{media} (private, writable).")
        else:
            self.add(FAIL, "Uploads folder", f"{media} is not writable by this account.")
        backups = Path(settings.BACKUP_ROOT).resolve()
        if not self._writable(backups):
            self.add(FAIL, "Backup folder", f"{backups} is not writable by this account.")
            return
        same_disk = self._same_disk(backups, media) or self._same_disk(backups, base)
        if same_disk and not settings.BACKUP_COPY_TO:
            self.add(WARN, "Backup folder", f"{backups} is on the same disk as the system and BACKUP_COPY_TO is not "
                                            "set: one disk failure would lose both. Set BACKUP_COPY_TO.")
        else:
            self.add(OK, "Backup folder", f"{backups}" + (f"; copies go to {settings.BACKUP_COPY_TO}"
                                                         if settings.BACKUP_COPY_TO else ""))
        free = shutil.disk_usage(backups).free
        if free < 2 * 1024 ** 3:
            self.add(WARN, "Disk space", f"Only {free / 1024 ** 3:.1f} GB free for backups.")

    def check_static(self):
        manifest = Path(settings.STATIC_ROOT) / "staticfiles.json"
        if settings.DEBUG:
            self.add(OK, "Static files", "Served from the source folders (debug mode).")
        elif manifest.exists():
            self.add(OK, "Static files", "Collected.")
        else:
            self.add(FAIL, "Static files", "Not collected. Run: manage.py collectstatic --noinput")

    def check_backups(self):
        from apps.backups import engine, services

        if connection.vendor == "postgresql":
            try:
                self.add(OK, "pg_dump", engine.pg_tool("pg_dump"))
            except engine.BackupError as exc:
                self.add(FAIL, "pg_dump", str(exc))
        h = services.health()
        if h["last"] is None:
            self.add(WARN, "Backups", "No backup has been made yet. Run: manage.py backup --verify")
        elif h["overdue"]:
            self.add(WARN, "Backups", f"Last successful backup {h['last'].started_at:%d %b %Y %H:%M}: more than a "
                                      "day ago. Check the HotelIT Backup scheduled task.")
        else:
            self.add(OK, "Backups", f"Last successful backup {h['last'].started_at:%d %b %Y %H:%M}.")
        if h["last"] and not h["last_verified"]:
            self.add(WARN, "Backup verification", "No backup has been verified yet. Run: manage.py verify_backup")

    def check_accounts(self):
        from apps.accounts.roles import Role

        users = get_user_model().objects.filter(is_active=True)
        admins = users.filter(profile__role=Role.ADMIN).count()
        if not admins:
            self.add(FAIL, "Administrators", "No active administrator account.")
        else:
            self.add(OK, "Administrators", f"{admins} active.")

    def check_demo_data(self):
        from apps.helpdesk.models import Ticket

        demo_users = get_user_model().objects.filter(username__startswith="demo_", is_active=True).count()
        demo_tickets = Ticket.objects.filter(subject__startswith="[DEMO]").count()
        if demo_users or demo_tickets:
            self.add(self.strict(), "Demo data", f"{demo_users} active demo_* account(s) and {demo_tickets} [DEMO] "
                                                 "ticket(s). Production must start from an empty database; demo "
                                                 "accounts have a published password.")
        else:
            self.add(OK, "Demo data", "None found.")

    def check_email(self):
        from apps.core.models import SystemSetting

        if SystemSetting.get().email_notifications_enabled and "console" in settings.EMAIL_BACKEND:
            self.add(WARN, "Email", "Email notifications are switched on but EMAIL_BACKEND is the console "
                                    "backend, so nothing is sent.")

    # ------------------------------------------------------------------
    @staticmethod
    def _writable(path):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".preflight-probe"
            probe.write_text("ok")
            probe.unlink()
            return True
        except OSError:
            return False

    @staticmethod
    def _same_disk(a, b):
        if os.name == "nt":
            return a.drive.lower() == b.drive.lower()
        try:
            return a.stat().st_dev == b.stat().st_dev
        except OSError:
            return False
