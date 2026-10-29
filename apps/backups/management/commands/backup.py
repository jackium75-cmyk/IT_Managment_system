from django.core.management.base import BaseCommand, CommandError

from apps.backups import engine, services
from apps.backups.models import BackupRecord


class Command(BaseCommand):
    help = ("Back up the database and uploaded files into BACKUP_ROOT, copy to BACKUP_COPY_TO if set, and remove "
            "old backups according to BACKUP_KEEP_DAILY / BACKUP_KEEP_MONTHLY. Schedule it nightly.")

    def add_arguments(self, parser):
        parser.add_argument("--verify", action="store_true", help="Verify the new backup (test restore) afterwards.")
        parser.add_argument("--scheduled", action="store_true", help="Record the backup as a scheduled one.")
        parser.add_argument("--list", action="store_true", help="List recorded backups instead of making one.")

    def handle(self, *args, **opts):
        services.sync_records()
        if opts["list"]:
            for r in BackupRecord.objects.all()[:50]:
                self.stdout.write(f"{r.started_at:%Y-%m-%d %H:%M}  {r.get_status_display():<10} "
                                  f"{r.get_verify_status_display():<20} {engine.human_size(r.size_bytes):>9}  "
                                  f"{r.file_name}")
            return
        kind = BackupRecord.Kind.SCHEDULED if opts["scheduled"] else BackupRecord.Kind.MANUAL
        try:
            record = services.run_backup(kind, verify=opts["verify"])
        except services.BackupBusy as exc:
            raise CommandError(str(exc))
        if record.status != BackupRecord.Status.SUCCESS:
            raise CommandError(f"Backup failed: {record.message}")
        self.stdout.write(self.style.SUCCESS(f"Backup written: {services.backup_root() / record.file_name}"))
        self.stdout.write(f"  {record.message}  SHA-256 {record.sha256}")
        if record.copied_to:
            self.stdout.write(f"  Copied to {record.copied_to}")
        if record.copy_error:
            self.stderr.write(self.style.WARNING(f"  {record.copy_error}"))
        if opts["verify"]:
            self.stdout.write(f"  Verification: {record.get_verify_status_display()}")
            for line in record.verify_message.splitlines():
                self.stdout.write(f"    {line}")
            if record.verify_status != BackupRecord.Verify.OK:
                raise CommandError("The backup was written but failed verification.")
