from django.core.management.base import BaseCommand, CommandError

from apps.backups import engine, services
from apps.backups.models import BackupRecord

from ._common import resolve_backup


class Command(BaseCommand):
    help = ("Verify a backup: whole-file fingerprint, every file in the archive, and a test restore of the "
            "database (SQLite: into a temporary file; PostgreSQL: into BACKUP_VERIFY_DB if set). "
            "Without a file name the newest backup is verified.")

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", help="Backup file name in BACKUP_ROOT, or a full path (e.g. on the NAS).")
        parser.add_argument("--no-restore-test", action="store_true", help="Only check fingerprints.")

    def handle(self, *args, **opts):
        services.sync_records()
        if opts["file"]:
            path = resolve_backup(opts["file"])
        else:
            last = services.last_success()
            if not last:
                raise CommandError("No backups recorded yet. Run: python manage.py backup")
            path = services.backup_root() / last.file_name
        record = BackupRecord.objects.filter(file_name=path.name, status=BackupRecord.Status.SUCCESS).first()
        if record and path.parent == services.backup_root().resolve() and not opts["no_restore_test"]:
            record = services.run_verify(record)
            ok, summary = record.verify_status == BackupRecord.Verify.OK, record.verify_message
        else:
            result = engine.verify_archive(path, restore_test=not opts["no_restore_test"])
            ok, summary = result.ok, result.summary
        self.stdout.write(f"Backup: {path}")
        for line in summary.splitlines():
            self.stdout.write(f"  {line}")
        if not ok:
            raise CommandError("Verification FAILED.")
        self.stdout.write(self.style.SUCCESS("Verification passed."))
