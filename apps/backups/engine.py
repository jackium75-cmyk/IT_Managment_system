"""
Creating, verifying and restoring backup archives.

A backup is a single zip file in BACKUP_ROOT::

    manifest.json           what is inside: SHA-256 fingerprints, sizes, row counts, migrations
    database/db.sqlite3     (SQLite)  or  database/database.dump  (PostgreSQL custom format)
    media/...               the private uploads folder (evidence and attachments)

plus a sidecar ``<name>.zip.sha256`` holding the fingerprint of the whole zip, in the
format used by ``sha256sum`` (Windows: compare with ``Get-FileHash``).

The database snapshot is consistent: SQLite uses its online backup API, PostgreSQL
runs ``pg_dump`` against a snapshot exported from the same transaction that counts
the rows, so the row counts in the manifest describe exactly what was dumped.

This module only deals with files and databases. Records, audit entries, rotation
and copying live in ``services``.
"""
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

import django
from django.conf import settings
from django.db import connection
from django.utils import timezone

FORMAT = 1
PREFIX = "hotel-it-backup-"
CHUNK = 1024 * 1024
# Already-compressed formats are stored as they are; compressing them again only costs time.
STORED_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov", ".m4v", ".webm", ".zip",
                   ".docx", ".xlsx", ".pptx", ".pdf", ".dump"}
PG_TIMEOUT = 6 * 60 * 60


class BackupError(Exception):
    """A backup, verification or restore could not be completed. The message is shown to administrators."""


@dataclass
class VerifyResult:
    ok: bool = True
    notes: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    manifest: dict = None

    def fail(self, text):
        self.ok = False
        self.problems.append(text)

    @property
    def summary(self):
        return "\n".join(self.problems + self.notes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sidecar_path(path):
    return Path(f"{path}.sha256")


def human_size(n):
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _safe_member(name):
    """Zip entries must be plain relative paths inside the expected folders (no zip-slip)."""
    if "\\" in name or ":" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if not parts or ".." in parts:
        return False
    return name == "manifest.json" or (parts[0] in ("database", "media") and len(parts) > 1)


def _extract(zf, member, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, CHUNK)


def _add_file(zf, src, arcname):
    h = hashlib.sha256()
    size = 0
    info = zipfile.ZipInfo.from_file(src, arcname)
    info.compress_type = zipfile.ZIP_STORED if Path(src).suffix.lower() in STORED_SUFFIXES else zipfile.ZIP_DEFLATED
    with open(src, "rb") as f, zf.open(info, "w", force_zip64=True) as out:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return {"path": arcname, "size": size, "sha256": h.hexdigest()}


def _media_files():
    root = Path(settings.MEDIA_ROOT)
    if not root.exists():
        return []
    return [(p, p.relative_to(root).as_posix()) for p in sorted(root.rglob("*")) if p.is_file()]


def _applied_migrations():
    from django.db.migrations.recorder import MigrationRecorder

    return sorted(f"{app}.{name}" for app, name in MigrationRecorder(connection).applied_migrations())


def migration_problem(manifest):
    """A backup made by a newer version of the code cannot be restored by this one."""
    from django.db.migrations.loader import MigrationLoader

    known = {f"{app}.{name}" for app, name in MigrationLoader(None, ignore_no_migrations=True).disk_migrations}
    unknown = sorted(set(manifest.get("migrations", [])) - known)
    if unknown:
        return (f"The backup was made by a newer version of the system ({len(unknown)} database change(s) this "
                f"version does not know, e.g. {unknown[0]}). Install the matching version first, then restore.")
    return ""


def _compare_counts(expected, actual):
    problems = []
    for table, n in sorted(expected.items()):
        if table not in actual:
            problems.append(f"Table {table} is missing after the test restore.")
        elif actual[table] != n:
            problems.append(f"Table {table}: {actual[table]} rows after the test restore, {n} expected.")
    return problems


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def _sqlite_counts(path):
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()


def _sqlite_snapshot(dest):
    connection.ensure_connection()
    target = sqlite3.connect(dest)
    try:
        connection.connection.backup(target)
    finally:
        target.close()
    return _sqlite_counts(dest)


def _sqlite_database_size():
    name = connection.settings_dict["NAME"]
    try:
        return Path(name).stat().st_size
    except (OSError, TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
def pg_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    if settings.PG_BIN_DIR:
        path = Path(settings.PG_BIN_DIR) / exe
        if path.exists():
            return str(path)
        raise BackupError(f"{exe} was not found in PG_BIN_DIR ({settings.PG_BIN_DIR}).")
    found = shutil.which(name)
    if not found:
        raise BackupError(f"{name} was not found. Install the PostgreSQL client tools or set PG_BIN_DIR in .env.")
    return found


def _pg_params(dbname=None):
    s = connection.settings_dict
    params = {"host": s["HOST"] or "localhost", "port": str(s["PORT"] or 5432), "user": s["USER"],
              "dbname": dbname or s["NAME"]}
    if s["PASSWORD"]:
        params["password"] = s["PASSWORD"]
    if (s.get("OPTIONS") or {}).get("sslmode"):
        params["sslmode"] = s["OPTIONS"]["sslmode"]
    return params


def _pg_env(dbname=None):
    """Connection settings for the client tools. The password goes in the environment, never on the command line."""
    p = _pg_params(dbname)
    env = os.environ.copy()
    env.update({"PGHOST": p["host"], "PGPORT": p["port"], "PGUSER": p["user"], "PGDATABASE": p["dbname"]})
    if "password" in p:
        env["PGPASSWORD"] = p["password"]
    if "sslmode" in p:
        env["PGSSLMODE"] = p["sslmode"]
    return env


def _run(args, env, what):
    try:
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=PG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"{what} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise BackupError(f"{what} failed: " + " ".join(detail))
    return proc.stdout


def _pg_connect(dbname=None):
    import psycopg

    return psycopg.connect(**_pg_params(dbname))


def _pg_counts(cur):
    from psycopg import sql

    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    tables = [r[0] for r in cur.fetchall()]
    counts = {}
    for t in tables:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(t)))
        counts[t] = cur.fetchone()[0]
    return counts


def _pg_snapshot(dest):
    import psycopg

    with _pg_connect() as con:
        con.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with con.cursor() as cur:
            cur.execute("SELECT pg_export_snapshot()")
            snapshot = cur.fetchone()[0]
            counts = _pg_counts(cur)
            # The transaction stays open while pg_dump runs, so it sees exactly the same data.
            _run([pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges", f"--snapshot={snapshot}",
                  f"--file={dest}"], _pg_env(), "pg_dump")
        con.rollback()
    return counts


def _pg_database_size():
    with connection.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        return cur.fetchone()[0]


def _pg_reset_schema(dbname):
    with _pg_connect(dbname) as con:
        con.execute("DROP SCHEMA IF EXISTS public CASCADE")
        con.execute("CREATE SCHEMA public")
        con.commit()


def _pg_restore(dump, dbname):
    _pg_reset_schema(dbname)
    _run([pg_tool("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
          f"--dbname={dbname}", str(dump)], _pg_env(dbname), "pg_restore")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def _check_space(directory):
    db_size = _sqlite_database_size() if connection.vendor == "sqlite" else _pg_database_size()
    media = sum(p.stat().st_size for p, _rel in _media_files())
    needed = 2 * db_size + media + 50 * 1024 * 1024  # temporary database copy + archive + margin
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise BackupError(f"Not enough free space in {directory}: {human_size(free)} free, about "
                          f"{human_size(needed)} needed. Free some space or move BACKUP_ROOT.")


def archive_name(now=None):
    import zoneinfo

    from apps.core.models import SystemSetting

    try:
        tz = zoneinfo.ZoneInfo(SystemSetting.get().hotel_timezone)
    except Exception:  # noqa: BLE001 - fall back to the server setting
        tz = None
    now = timezone.localtime(now or timezone.now(), tz)
    return f"{PREFIX}{now:%Y%m%d-%H%M%S}.zip"


def create_archive(directory, now=None, record_id=None):
    """Write a new backup into ``directory``. Returns a dict with path, size, sha256 and manifest.

    ``record_id`` is the BackupRecord describing this backup. The snapshot contains that record while it is
    still "running", so it is stored here to complete the record after a restore.
    """
    from apps.core.models import SystemSetting

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    vendor = connection.vendor
    if vendor not in ("sqlite", "postgresql"):
        raise BackupError(f"Backups are not supported for the {vendor} database engine.")
    _check_space(directory)
    final = directory / archive_name(now)
    n = 2
    while final.exists():
        final = directory / archive_name(now).replace(".zip", f"-{n}.zip")
        n += 1
    partial = Path(f"{final}.partial")
    manifest = {
        "format": FORMAT,
        "application": "Hotel IT Management System",
        "hotel_name": SystemSetting.get().hotel_name,
        "created_at": timezone.now().isoformat(),
        "host": platform.node(),
        "django": django.get_version(),
        "db_engine": vendor,
        "migrations": _applied_migrations(),
        "record_id": record_id,
    }
    with tempfile.TemporaryDirectory(dir=directory, prefix=".tmp-") as tmp:
        db_file = Path(tmp) / ("db.sqlite3" if vendor == "sqlite" else "database.dump")
        manifest["row_counts"] = _sqlite_snapshot(db_file) if vendor == "sqlite" else _pg_snapshot(db_file)
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                                 allowZip64=True) as zf:
                manifest["database"] = _add_file(zf, db_file, f"database/{db_file.name}")
                manifest["files"] = [_add_file(zf, path, f"media/{rel}") for path, rel in _media_files()]
                zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    digest = sha256_file(partial)
    os.replace(partial, final)
    sidecar_path(final).write_text(f"{digest}  {final.name}\n", encoding="ascii")
    return {"path": final, "size": final.stat().st_size, "sha256": digest, "manifest": manifest}


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
def read_manifest(zf):
    try:
        manifest = json.loads(zf.read("manifest.json"))
    except KeyError:
        raise BackupError("manifest.json is missing: this is not a Hotel IT backup.")
    except ValueError:
        raise BackupError("manifest.json is damaged.")
    if manifest.get("format") != FORMAT or "database" not in manifest:
        raise BackupError("Unsupported backup format.")
    return manifest


def verify_archive(path, *, restore_test=True):
    """Check fingerprints, every file in the archive, and (by default) test-restore the database."""
    path = Path(path)
    result = VerifyResult()
    if not path.is_file():
        result.fail(f"{path} does not exist.")
        return result
    digest = sha256_file(path)
    sidecar = sidecar_path(path)
    if sidecar.exists():
        expected = (sidecar.read_text(encoding="ascii", errors="replace").split() or [""])[0].lower()
        if expected != digest:
            result.fail("The whole-file fingerprint does not match the .sha256 file: the backup is damaged or "
                        "was altered.")
        else:
            result.notes.append("Whole-file SHA-256 fingerprint matches.")
    else:
        result.notes.append("No .sha256 file next to the backup, so the whole-file fingerprint was not checked.")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        result.fail(f"Not a readable zip archive ({exc}).")
        return result
    with zf:
        names = set(zf.namelist())
        unsafe = sorted(n for n in names if not _safe_member(n))
        if unsafe:
            result.fail(f"The archive contains unexpected or unsafe paths (e.g. {unsafe[0]}); it will not be used.")
            return result
        try:
            manifest = result.manifest = read_manifest(zf)
        except BackupError as exc:
            result.fail(str(exc))
            return result
        entries = [manifest["database"]] + manifest.get("files", [])
        listed = {e["path"] for e in entries}
        for extra in sorted(names - listed - {"manifest.json"})[:5]:
            result.fail(f"{extra} is in the archive but not in the manifest.")
        bad = 0
        for e in entries:
            if e["path"] not in names:
                result.fail(f"{e['path']} is listed in the manifest but missing.")
                continue
            h = hashlib.sha256()
            try:
                with zf.open(e["path"]) as f:
                    for chunk in iter(lambda: f.read(CHUNK), b""):
                        h.update(chunk)
            except (zipfile.BadZipFile, OSError) as exc:  # CRC errors are raised here
                result.fail(f"{e['path']} cannot be read ({exc}).")
                continue
            if h.hexdigest() != e["sha256"]:
                bad += 1
                if bad <= 5:
                    result.fail(f"{e['path']} does not match its fingerprint.")
        if bad > 5:
            result.fail(f"…and {bad - 5} more file(s) do not match.")
        if result.ok:
            result.notes.append(f"The database and {len(entries) - 1} uploaded file(s) match their fingerprints.")
        if result.ok and restore_test:
            try:
                _restore_test(zf, path, manifest, result)
            except BackupError as exc:
                result.fail(str(exc))
    return result


def _restore_test(zf, path, manifest, result):
    expected = manifest.get("row_counts", {})
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".verify-") as tmp:
        db = Path(tmp) / PurePosixPath(manifest["database"]["path"]).name
        _extract(zf, manifest["database"]["path"], db)
        if manifest["db_engine"] == "sqlite":
            con = sqlite3.connect(db)
            try:
                check = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            if check != "ok":
                result.fail(f"SQLite integrity check failed: {check}")
                return
            problems = _compare_counts(expected, _sqlite_counts(db))
        else:
            _run([pg_tool("pg_restore"), "--list", str(db)], _pg_env(), "pg_restore --list")
            scratch = settings.BACKUP_VERIFY_DB
            if not scratch:
                result.notes.append("PostgreSQL archive structure is readable. Set BACKUP_VERIFY_DB to also "
                                    "test-restore it into a scratch database.")
                return
            if connection.vendor == "postgresql" and scratch == connection.settings_dict["NAME"]:
                raise BackupError("BACKUP_VERIFY_DB must be a separate scratch database, not the live one.")
            _pg_restore(db, scratch)
            with _pg_connect(scratch) as con, con.cursor() as cur:
                problems = _compare_counts(expected, _pg_counts(cur))
        for p in problems[:10]:
            result.fail(p)
        if not problems:
            total = sum(expected.values())
            result.notes.append(f"Test restore succeeded: {len(expected)} tables and {total} rows match the "
                                "counts taken at backup time.")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def restore_archive(path, *, before_swap=None):
    """Replace the live database and uploads with the contents of a verified backup.

    The caller must have stopped the web server and made a safety backup first.
    Returns the folder the previous uploads were moved to (or None).
    """
    path = Path(path)
    result = verify_archive(path)
    if not result.ok:
        raise BackupError("The backup failed verification and was not restored:\n" + result.summary)
    manifest = result.manifest
    if manifest["db_engine"] != connection.vendor:
        raise BackupError(f"This backup is from a {manifest['db_engine']} database but this installation uses "
                          f"{connection.vendor}. Use the matching DB_ENGINE.")
    problem = migration_problem(manifest)
    if problem:
        raise BackupError(problem)
    media_root = Path(settings.MEDIA_ROOT)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging = media_root.with_name(f"{media_root.name}.restoring-{stamp}")
    try:
        with zipfile.ZipFile(path) as zf, tempfile.TemporaryDirectory(dir=path.parent, prefix=".restore-") as tmp:
            db = Path(tmp) / PurePosixPath(manifest["database"]["path"]).name
            _extract(zf, manifest["database"]["path"], db)
            staging.mkdir(parents=True)
            for e in manifest.get("files", []):
                _extract(zf, e["path"], staging.joinpath(*PurePosixPath(e["path"]).parts[1:]))
            if before_swap:
                before_swap()
            if connection.vendor == "sqlite":
                source = sqlite3.connect(db)
                try:
                    connection.ensure_connection()
                    source.backup(connection.connection)
                finally:
                    source.close()
            else:
                connection.close()
                _pg_restore(db, connection.settings_dict["NAME"])
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # the live uploads folder has not been touched yet
        raise
    previous = None
    if media_root.exists():
        previous = media_root.with_name(f"{media_root.name}.before-restore-{stamp}")
        os.replace(media_root, previous)
    os.replace(staging, media_root)
    return previous
