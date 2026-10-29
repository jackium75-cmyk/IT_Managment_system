# Backup and restore

## What a backup contains

Each backup is one file, `hotel-it-backup-YYYYMMDD-HHMMSS.zip`, in `BACKUP_ROOT`. It contains:

| Inside the zip | What |
|---|---|
| `database/database.dump` (PostgreSQL) or `database/db.sqlite3` (SQLite) | Every record: tickets, tasks, time, assets, projects, users, audit log, settings. |
| `media/…` | Every uploaded photo, video and document, with its original bytes. |
| `manifest.json` | When and where it was made; a SHA-256 fingerprint and size for every file; row counts per table; the database version (migrations). |

Next to it, `….zip.sha256` holds the fingerprint of the whole zip.

The database copy is **consistent**:
* On PostgreSQL, `pg_dump` and the row counts use the same database snapshot, so nothing changes half-way.
* On SQLite, the online backup API is used.

Staff can keep working during a backup.

**Not included**, because it holds secrets:
* `.env`, with the secret key and the database password. Keep a copy in the hotel's password manager or safe.
* The application code. Keep the release you installed.

## Schedule and retention

* The **HotelIT Backup** scheduled task runs nightly at 02:30. It runs `manage.py backup --scheduled --verify` (see [DEPLOYMENT.md](DEPLOYMENT.md#6-scheduled-tasks-server-jobs-backups)).
* Old backups are removed automatically. These are kept:
  * the newest `BACKUP_KEEP_DAILY` backups (default 14);
  * the newest backup of each of the last `BACKUP_KEEP_MONTHLY` months (default 12);
  * always the newest backup that passed verification.
* Administrators can also click **Back up now** under *Administration → Backups*.
* If a backup, its copy or its verification fails:
  * administrators get a notification;
  * the dashboard card *Last successful backup* turns red after 26 hours without a successful backup;
  * `manage.py preflight` reports it.

## The second copy (do not skip this)

A backup on the same disk as the database is lost together with it. Set `BACKUP_COPY_TO` to another disk or a NAS share, for example `\\nas\backups\hotel-it`.
* Each new backup is copied there. The copy's fingerprint is checked, and rotation removes old copies too.
* The account that runs the scheduled task needs write access to the share.

Also take a copy **off site** regularly, for example a rotating USB disk kept in the safe. Fire, theft and ransomware usually affect everything in the building.

## Protecting backups

A backup contains every record and the password hashes. Treat it like the server itself:

* The install script restricts `BACKUP_ROOT` to Administrators, SYSTEM and the service account.
* Turn on **BitLocker** for the backup disk and any USB disk taken off site.
* Restrict the NAS share to the service account and IT administrators.
* Backups can **never** be downloaded through the web interface.

## Verifying backups

Verification runs after every scheduled backup. To run it by hand, use **Verify** under *Administration → Backups*, or:

```powershell
.\.venv\Scripts\python.exe manage.py verify_backup                               # newest backup
.\.venv\Scripts\python.exe manage.py verify_backup hotel-it-backup-20261029-023000.zip
.\.venv\Scripts\python.exe manage.py verify_backup \\nas\backups\hotel-it\hotel-it-backup-20261029-023000.zip
```

It checks:

1. the whole-file fingerprint against the `.sha256` file;
2. every file inside the zip against the fingerprint in the manifest (detects damage and tampering);
3. that the archive contains no unexpected or unsafe paths;
4. a **test restore** of the database, compared table by table with the row counts taken at backup time:
   * **SQLite:** restored into a temporary file, plus an integrity check;
   * **PostgreSQL:** restored into the scratch database `BACKUP_VERIFY_DB`. Its contents are replaced every time, so never point it at the live database; the system refuses to.
     If `BACKUP_VERIFY_DB` is not set, only the archive structure is checked (`pg_restore --list`).

## Restoring on the same server

Use this after data loss, corruption, or a failed update.

1. Tell staff the system will be unavailable.
2. In **PowerShell as Administrator**, stop everything:

   ```powershell
   Stop-ScheduledTask "HotelIT Server"; Stop-ScheduledTask "HotelIT Jobs"; Stop-ScheduledTask "HotelIT Backup"
   Disable-ScheduledTask "HotelIT Jobs"; Disable-ScheduledTask "HotelIT Backup"
   ```

3. List the backups and choose one:

   ```powershell
   cd C:\HotelIT\hotel-it-system
   .\.venv\Scripts\python.exe manage.py backup --list
   ```

4. Restore:

   ```powershell
   .\.venv\Scripts\python.exe manage.py restore_backup hotel-it-backup-20261029-023000.zip
   ```

   The command:
   * **verifies** the backup first and changes nothing if it fails;
   * refuses a backup made by a **newer version** of the system (install that version first);
   * shows what will be restored and asks you to type `RESTORE`;
   * makes a **safety backup** of the current state, so the restore itself can be undone;
   * replaces the database;
   * swaps in the restored uploads folder. The current one is kept as `media_private.before-restore-<time>`;
   * applies any newer migrations, and records the restore in the audit log.

5. Start everything again:

   ```powershell
   Enable-ScheduledTask "HotelIT Jobs"; Enable-ScheduledTask "HotelIT Backup"
   Start-ScheduledTask "HotelIT Server"
   ```

6. Sign in and check some recent tickets, and open an attachment. Once satisfied, delete the `media_private.before-restore-…` folder.

**Anything recorded after the backup was made is not in the restored system.**
* Ask staff to re-enter important work from that period.
* The safety backup still holds it if you need to look something up: restore it on a test machine.

If the restore fails part-way:
* On PostgreSQL the database restore runs as a single transaction, but the old schema has already been removed.
* Restore the **safety backup** that the command named, in the same way.

## Restoring onto a new server (disaster recovery)

1. Build the new server as in [DEPLOYMENT.md](DEPLOYMENT.md), sections 1–4:
   * install Python and PostgreSQL;
   * create the `hotel_it` database and user;
   * install the **same version** of the application, or a newer one;
   * restore the saved `.env` and adjust paths and addresses if they changed.
   Do not create users and do not load demo data.
2. Copy the backup zip and its `.sha256` file from the NAS or USB disk into `BACKUP_ROOT`.
3. Run:

   ```powershell
   .\.venv\Scripts\python.exe manage.py migrate
   .\.venv\Scripts\python.exe manage.py restore_backup hotel-it-backup-YYYYMMDD-HHMMSS.zip --no-safety-backup
   ```

   `--no-safety-backup` is acceptable here because the new database is empty.
4. Register the scheduled tasks (section 6 of the deployment guide).
5. Run `manage.py preflight`.

If the original `.env` is lost:
* Generate a new `DJANGO_SECRET_KEY`. Everyone simply has to sign in again.
* Set a new database password with `ALTER USER hotel_it PASSWORD '…'`.

## Restore drill (every 3 months)

A backup that has never been restored is a hope, not a backup. Every quarter:

1. On a spare PC or virtual machine, follow *Restoring onto a new server* with the latest copy from the NAS.
2. Sign in, open a recent ticket with a photo, and run a monthly report.
3. Record the date and result in the IT diary.

## Command reference

| Command | Purpose |
|---|---|
| `manage.py backup` | Make a backup now: copy, then rotate. |
| `manage.py backup --verify` | …and verify it. The scheduled task uses `--scheduled --verify`. |
| `manage.py backup --list` | List recorded backups. |
| `manage.py verify_backup [file]` | Verify the newest backup, or the named file or path. |
| `manage.py verify_backup FILE --no-restore-test` | Only check fingerprints (fast). |
| `manage.py restore_backup FILE` | Restore, interactively. |
| `manage.py restore_backup FILE --yes` | Restore without the prompt, for scripted disaster-recovery tests. |
| `manage.py preflight` | Readiness check, including backup age and verification. |
