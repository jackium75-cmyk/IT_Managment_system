from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.backups import engine, services
from apps.backups.models import BackupRecord
from apps.core import audit
from apps.core.models import AuditLog

from ._common import resolve_backup


class Command(BaseCommand):
    help = ("Replace the live database and uploaded files with a backup. Stop the web server and the scheduled "
            "tasks first. A safety backup of the current state is made before anything is changed.")

    def add_arguments(self, parser):
        parser.add_argument("file", help="Backup file name in BACKUP_ROOT, or a full path.")
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation (for scripted use).")
        parser.add_argument("--no-safety-backup", action="store_true",
                            help="Skip the safety backup of the current state (only if the live database is "
                                 "already unusable).")

    def handle(self, *args, **opts):
        path = resolve_backup(opts["file"])
        self.stdout.write(f"Checking {path} …")
        result = engine.verify_archive(path)
        for line in result.summary.splitlines():
            self.stdout.write(f"  {line}")
        if not result.ok:
            raise CommandError("The backup failed verification. Nothing was changed.")
        m = result.manifest
        problem = engine.migration_problem(m)
        if problem:
            raise CommandError(problem)
        self.stdout.write(self.style.WARNING(
            f"\nThis will REPLACE all current data with the backup made {m['created_at']} on {m.get('host', '?')} "
            f"for {m.get('hotel_name', '?')} ({sum(m.get('row_counts', {}).values())} rows, "
            f"{len(m.get('files', []))} uploaded files).\n"
            "Anything recorded after that time will no longer be in the system.\n"
            "Make sure the web server and the scheduled tasks are STOPPED."))
        if not opts["yes"]:
            if not self.stdin_is_interactive():
                raise CommandError("Run interactively, or pass --yes to confirm.")
            if input('Type RESTORE to continue: ').strip() != "RESTORE":
                raise CommandError("Cancelled. Nothing was changed.")
        safety = None
        if not opts["no_safety_backup"]:
            self.stdout.write("Making a safety backup of the current state …")
            safety = services.run_backup(BackupRecord.Kind.PRE_RESTORE)
            if safety.status != BackupRecord.Status.SUCCESS:
                raise CommandError(f"The safety backup failed, so nothing was restored: {safety.message}")
            self.stdout.write(f"  Safety backup: {services.backup_root() / safety.file_name}")
        self.stdout.write("Restoring …")
        try:
            previous_media = engine.restore_archive(path)
        except engine.BackupError as exc:
            hint = f" Restore the safety backup {safety.file_name} the same way." if safety else ""
            raise CommandError(f"Restore failed: {exc}.{hint}")
        cache.clear()
        call_command("migrate", interactive=False, verbosity=0)
        services.after_restore(path, m)
        audit.log(AuditLog.Action.BACKUP, message=f"Database and uploads restored from {path.name}"
                  + (f"; safety backup {safety.file_name}" if safety else ""))
        self.stdout.write(self.style.SUCCESS("Restore complete."))
        if previous_media:
            self.stdout.write(f"The previous uploads folder was kept as {previous_media}. Delete it once you have "
                              "checked the restored system.")
        self.stdout.write("Start the web server again and check a few recent records.")

    def stdin_is_interactive(self):
        import sys

        return sys.stdin.isatty()
