from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError


def resolve_backup(name):
    """Accept a full path, or a file name inside BACKUP_ROOT."""
    path = Path(name)
    if not path.exists() and not path.is_absolute():
        path = Path(settings.BACKUP_ROOT) / name
    if not path.is_file():
        raise CommandError(f"Backup file not found: {name}")
    return path.resolve()
