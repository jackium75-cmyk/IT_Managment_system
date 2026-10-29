import io
import json
import shutil
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department
from apps.accounts.roles import Role
from apps.core.models import AuditLog
from apps.core.testing import PASSWORD
from apps.notifications.models import Notification

from . import engine, services
from .models import BackupRecord

R = BackupRecord


class BackupTestCase(TransactionTestCase):
    """TransactionTestCase: the SQLite backup API must see committed data, as in production."""

    def setUp(self):
        cache.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="hotel-it-backup-test-"))
        self.media = self.tmp / "media"
        self.root = self.tmp / "backups"
        self.copy = self.tmp / "nas"
        (self.media / "evidence").mkdir(parents=True)
        (self.media / "evidence" / "photo.jpg").write_bytes(b"\xff\xd8\xff original photo")
        self.override = override_settings(MEDIA_ROOT=self.media, BACKUP_ROOT=self.root, BACKUP_COPY_TO=str(self.copy),
                                          BACKUP_RUN_IN_THREAD=False, BACKUP_KEEP_DAILY=14, BACKUP_KEEP_MONTHLY=12)
        self.override.enable()
        self.admin = self.make_user("adm", Role.ADMIN)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_user(self, username, role=Role.EMPLOYEE):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username=username, password=PASSWORD)
        user.profile.role = role
        user.profile.save()
        return user

    def backup(self, **kw):
        record = services.run_backup(R.Kind.MANUAL, self.admin, **kw)
        self.assertEqual(record.status, R.Status.SUCCESS, record.message)
        return record

    def path(self, record):
        return self.root / record.file_name


class CreateAndVerifyTests(BackupTestCase):
    def test_backup_contains_database_media_and_manifest(self):
        record = self.backup(verify=True)
        self.assertEqual(record.verify_status, R.Verify.OK, record.verify_message)
        sqlite = connection.vendor == "sqlite"
        # PostgreSQL without BACKUP_VERIFY_DB only checks the archive structure; with it, see PostgresVerifyTests.
        self.assertIn("Test restore succeeded" if sqlite else "archive structure is readable", record.verify_message)
        with zipfile.ZipFile(self.path(record)) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
        dump = "database/db.sqlite3" if sqlite else "database/database.dump"
        self.assertEqual(names, {"manifest.json", dump, "media/evidence/photo.jpg"})
        self.assertEqual(manifest["db_engine"], connection.vendor)
        self.assertEqual(manifest["record_id"], record.pk)
        self.assertEqual(manifest["row_counts"]["auth_user"], 1)
        self.assertIn("backups.0001_initial", manifest["migrations"])
        sidecar = engine.sidecar_path(self.path(record)).read_text()
        self.assertTrue(sidecar.startswith(record.sha256))
        self.assertEqual(engine.sha256_file(self.copy / record.file_name), record.sha256)
        self.assertEqual(record.copied_to, str(self.copy))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.BACKUP).exists())

    def test_damaged_or_altered_backups_fail_verification(self):
        record = self.backup()
        path = self.path(record)
        altered = self.tmp / "altered.zip"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(altered, "w") as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                dst.writestr(info, data + b"!" if info.filename.startswith("media/") else data)
        result = engine.verify_archive(altered)
        self.assertFalse(result.ok)
        self.assertIn("media/evidence/photo.jpg does not match its fingerprint.", result.problems)

        engine.sidecar_path(path).write_text("0" * 64 + "  x.zip\n")
        result = engine.verify_archive(path)
        self.assertFalse(result.ok)
        self.assertIn("damaged or was altered", result.summary)

    def test_unsafe_paths_are_refused(self):
        evil = self.tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("media/../../outside.txt", "x")
        result = engine.verify_archive(evil)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.summary)
        with self.assertRaises(engine.BackupError):
            engine.restore_archive(evil)
        self.assertFalse((self.tmp / "outside.txt").exists())

    def test_backup_from_newer_version_cannot_be_restored(self):
        self.assertIn("newer version", engine.migration_problem({"migrations": ["core.9999_future"]}))
        self.assertEqual(engine.migration_problem({"migrations": ["core.0001_initial"]}), "")

    def test_only_one_backup_at_a_time(self):
        with services._Lock():
            with self.assertRaises(services.BackupBusy):
                services.run_backup(R.Kind.MANUAL, self.admin)
        self.assertFalse(R.objects.exists())  # nothing recorded, nobody alerted

    def test_failure_is_recorded_and_admins_notified(self):
        with mock.patch.object(engine, "_check_space", side_effect=engine.BackupError("Not enough free space")):
            record = services.run_backup(R.Kind.SCHEDULED)
        self.assertEqual((record.status, record.message), (R.Status.FAILED, "Not enough free space"))
        self.assertTrue(Notification.objects.filter(recipient=self.admin, kind=Notification.Kind.SYSTEM).exists())
        self.assertFalse((self.root / services.LOCK_NAME).exists())


class RestoreTests(BackupTestCase):
    def test_restore_brings_back_database_and_uploads(self):
        record = self.backup()
        Department.objects.create(name="Added after backup", code="NEW")
        (self.media / "evidence" / "photo.jpg").write_bytes(b"changed")
        (self.media / "evidence" / "later.jpg").write_bytes(b"later")
        previous = engine.restore_archive(self.path(record))
        self.assertFalse(Department.objects.filter(code="NEW").exists())
        self.assertEqual((self.media / "evidence" / "photo.jpg").read_bytes(), b"\xff\xd8\xff original photo")
        self.assertFalse((self.media / "evidence" / "later.jpg").exists())
        self.assertTrue((previous / "evidence" / "later.jpg").exists())  # nothing is thrown away

    def test_failed_restore_leaves_uploads_untouched(self):
        record = self.backup()
        (self.media / "evidence" / "later.jpg").write_bytes(b"later")

        def fail():
            raise engine.BackupError("database server went away")

        with self.assertRaises(engine.BackupError):
            engine.restore_archive(self.path(record), before_swap=fail)
        self.assertTrue((self.media / "evidence" / "later.jpg").exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["backups", "media", "nas"])  # no leftovers

    def test_restore_command_makes_safety_backup_and_tidies_records(self):
        record = self.backup()
        Department.objects.create(name="Added after backup", code="NEW")
        out = io.StringIO()
        call_command("restore_backup", record.file_name, "--yes", stdout=out)
        self.assertIn("Restore complete", out.getvalue())
        self.assertFalse(Department.objects.filter(code="NEW").exists())
        restored = R.objects.get(file_name=record.file_name)
        self.assertEqual(restored.status, R.Status.SUCCESS)  # was "running" inside its own snapshot
        self.assertFalse(R.objects.filter(status=R.Status.RUNNING).exists())
        safety = [p for p in self.root.glob("*.zip") if p.name != record.file_name]
        self.assertEqual(len(safety), 1)
        self.assertTrue(R.objects.filter(file_name=safety[0].name).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.BACKUP, message__contains="restored").exists())

    def test_restore_refuses_damaged_backup_without_changing_anything(self):
        record = self.backup()
        Department.objects.create(name="Keep me", code="KEEP")
        engine.sidecar_path(self.path(record)).write_text("0" * 64 + "  x\n")
        with self.assertRaises(CommandError):
            call_command("restore_backup", record.file_name, "--yes", stdout=io.StringIO())
        self.assertTrue(Department.objects.filter(code="KEEP").exists())


class RotationTests(BackupTestCase):
    def fake(self, days_ago, verified=False):
        started = timezone.now() - timedelta(days=days_ago)
        name = f"{engine.PREFIX}{started:%Y%m%d-%H%M%S}.zip"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(b"zip")
        engine.sidecar_path(self.root / name).write_text("x")
        return R.objects.create(status=R.Status.SUCCESS, started_at=started, file_name=name,
                                verify_status=R.Verify.OK if verified else R.Verify.NOT_VERIFIED)

    @override_settings(BACKUP_KEEP_DAILY=3, BACKUP_KEEP_MONTHLY=0)
    def test_keeps_newest_and_newest_verified(self):
        old_verified = self.fake(40, verified=True)
        oldest = self.fake(50)
        recent = [self.fake(d) for d in (1, 2, 3, 4)]
        removed = services.rotate()
        self.assertEqual({r.pk for r in removed}, {oldest.pk, recent[3].pk})
        self.assertTrue((self.root / old_verified.file_name).exists())
        self.assertFalse((self.root / oldest.file_name).exists())
        self.assertFalse(engine.sidecar_path(self.root / oldest.file_name).exists())
        oldest.refresh_from_db()
        self.assertEqual(oldest.status, R.Status.ROTATED)

    @override_settings(BACKUP_KEEP_DAILY=1, BACKUP_KEEP_MONTHLY=3)
    def test_keeps_one_per_month(self):
        records = [self.fake(d) for d in (0, 1, 35, 36, 70, 200)]
        services.rotate()
        kept = set(R.objects.filter(status=R.Status.SUCCESS).values_list("pk", flat=True))
        self.assertEqual(kept, {records[0].pk, records[2].pk, records[4].pk})

    def test_sync_records_finds_copied_in_and_deleted_files(self):
        record = self.backup()
        copied_in = self.root / f"{engine.PREFIX}20200101-000000.zip"
        shutil.copyfile(self.path(record), copied_in)
        self.path(record).unlink()
        services.sync_records()
        self.assertEqual(R.objects.get(file_name=copied_in.name).kind, R.Kind.FOUND)
        record.refresh_from_db()
        self.assertEqual(record.status, R.Status.ROTATED)


class BackupPageTests(BackupTestCase):
    def test_admin_runs_backup_from_page(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("backups:list"))
        self.assertContains(resp, "No successful backup has been made yet")
        resp = self.client.post(reverse("backups:run"), follow=True)
        self.assertContains(resp, "Backup started")
        record = R.objects.get()
        self.assertEqual((record.status, record.verify_status, record.triggered_by),
                         (R.Status.SUCCESS, R.Verify.OK, self.admin))
        resp = self.client.post(reverse("backups:verify", args=[record.pk]), follow=True)
        self.assertContains(resp, "Verification of")
        self.assertNotContains(resp, "/download")

    def test_only_admins(self):
        for role in (Role.IT_OFFICER, Role.MANAGER, Role.EMPLOYEE):
            self.client.force_login(self.make_user(f"user_{role}", role))
            self.assertEqual(self.client.get(reverse("backups:list")).status_code, 403)
            self.assertEqual(self.client.post(reverse("backups:run")).status_code, 403)
        self.assertFalse(R.objects.exists())

    def test_interrupted_runs_are_marked_failed(self):
        R.objects.create(status=R.Status.RUNNING, started_at=timezone.now())  # process killed mid-backup
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("backups:list"))
        self.assertNotContains(resp, "This page refreshes")
        self.assertEqual(R.objects.get().status, R.Status.FAILED)

    def test_get_does_not_start_backup(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("backups:run")).status_code, 405)

    def test_dashboard_warns_when_no_recent_backup(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("dashboard:index"))
        self.assertContains(resp, "Last successful backup")
        self.assertContains(resp, "Never")


class CommandTests(BackupTestCase):
    def test_backup_and_verify_commands(self):
        out = io.StringIO()
        call_command("backup", "--verify", "--scheduled", stdout=out)
        self.assertIn("Verification: Verified", out.getvalue())
        self.assertEqual(R.objects.get().kind, R.Kind.SCHEDULED)
        out = io.StringIO()
        call_command("verify_backup", stdout=out)
        self.assertIn("Verification passed", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("verify_backup", "no-such-file.zip", stdout=io.StringIO())


@skipUnless(connection.vendor == "postgresql", "PostgreSQL only")
class PostgresVerifyTests(BackupTestCase):
    def test_test_restore_into_scratch_database(self):
        scratch = connection.settings_dict["NAME"] + "_verify"
        with connection.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
            cur.execute(f'CREATE DATABASE "{scratch}"')
        try:
            with override_settings(BACKUP_VERIFY_DB=scratch):
                record = self.backup(verify=True)
            self.assertEqual(record.verify_status, R.Verify.OK, record.verify_message)
            self.assertIn("Test restore succeeded", record.verify_message)
            with override_settings(BACKUP_VERIFY_DB=connection.settings_dict["NAME"]):
                result = engine.verify_archive(self.path(record))
            self.assertFalse(result.ok)
            self.assertIn("not the live one", result.summary)
        finally:
            with connection.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
