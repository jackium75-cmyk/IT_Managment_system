"""
Django settings for the Hotel IT Management System.

All secrets and environment-specific values are read from environment
variables (optionally loaded from a ``.env`` file in the project root).
See ``.env.example`` for every supported variable.
"""
from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        # Development-only fallback. Never used when DEBUG is off.
        SECRET_KEY = "dev-only-insecure-key-change-me"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false. "
            "Copy .env.example to .env and set a long random value."
        )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Project apps
    "apps.core",
    "apps.accounts",
    "apps.dashboard",
    "apps.diary",
    "apps.tasks",
    "apps.evidence",
    "apps.helpdesk",
    "apps.notifications",
    "apps.assets",
    "apps.maintenance",
    "apps.projects",
    "apps.procurement",
    "apps.knowledge",
    "apps.reports",
    "apps.backups",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.HotelTimezoneMiddleware",
    "apps.core.middleware.AuditRequestMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
    "apps.accounts.middleware.ForcePasswordChangeMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site_context",
                "apps.tasks.context_processors.running_timer",
                "apps.notifications.context_processors.unread_notifications",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database: SQLite for local development, PostgreSQL for production.
# ---------------------------------------------------------------------------
DB_ENGINE = env("DB_ENGINE", "sqlite").lower()
if DB_ENGINE in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "hotel_it"),
            "USER": env("DB_USER", "hotel_it"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "localhost"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
            "ATOMIC_REQUESTS": False,
        }
    }
elif DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / env("SQLITE_NAME", "db.sqlite3"),
        }
    }
else:
    raise ImproperlyConfigured(f"Unsupported DB_ENGINE: {DB_ENGINE!r}")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Authentication and passwords
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# Brute-force protection (see apps.accounts.throttling). These are startup
# defaults; administrators can override them in System Configuration.
LOGIN_MAX_FAILURES = env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_MINUTES = env_int("LOGIN_LOCKOUT_MINUTES", 15)

# ---------------------------------------------------------------------------
# Sessions, cookies and HTTP security headers
# ---------------------------------------------------------------------------
SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE_SECONDS", 8 * 60 * 60)  # one shift
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Enable these when the system is served over HTTPS (recommended).
USE_HTTPS = env_bool("DJANGO_USE_HTTPS", False)
SESSION_COOKIE_SECURE = USE_HTTPS
CSRF_COOKIE_SECURE = USE_HTTPS
SECURE_SSL_REDIRECT = USE_HTTPS
if USE_HTTPS:
    SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Content-Security-Policy, Permissions-Policy and no-store caching for signed-in
# pages are added by apps.core.middleware.SecurityHeadersMiddleware.

# HSTS preloading is for public internet domains, not a hotel's internal host name.
SILENCED_SYSTEM_CHECKS = ["security.W021"]

# ---------------------------------------------------------------------------
# Internationalisation and time
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-gb"
# All timestamps are stored in UTC. They are displayed in the hotel's
# timezone, which administrators can change in System Configuration.
TIME_ZONE = env("HOTEL_TIMEZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files (served by WhiteNoise, so no separate web server is needed)
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# ---------------------------------------------------------------------------
# Uploaded evidence. Stored OUTSIDE the static/web root and never served
# directly: every download goes through a permission-checked view.
# ---------------------------------------------------------------------------
MEDIA_ROOT = Path(env("PRIVATE_MEDIA_ROOT", str(BASE_DIR / "media_private")))
MEDIA_URL = "/protected-media/"  # Not routed; files are streamed by views.
FILE_UPLOAD_PERMISSIONS = 0o640
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024  # larger uploads stream to a temporary file
DATA_UPLOAD_MAX_NUMBER_FILES = 10
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# ---------------------------------------------------------------------------
# Backups (see apps.backups and docs/BACKUP_AND_RESTORE.md)
# ---------------------------------------------------------------------------
BACKUP_ROOT = Path(env("BACKUP_ROOT", str(BASE_DIR / "backups")))
# Optional second location (another disk or a NAS share such as \\nas\backups\hotel-it).
BACKUP_COPY_TO = env("BACKUP_COPY_TO", "")
BACKUP_KEEP_DAILY = env_int("BACKUP_KEEP_DAILY", 14)      # most recent backups kept
BACKUP_KEEP_MONTHLY = env_int("BACKUP_KEEP_MONTHLY", 12)  # plus the newest backup of each recent month
# PostgreSQL client tools folder, e.g. C:\Program Files\PostgreSQL\16\bin (empty = search PATH).
PG_BIN_DIR = env("PG_BIN_DIR", "")
# Optional scratch PostgreSQL database used to test-restore backups. Its contents are replaced.
BACKUP_VERIFY_DB = env("BACKUP_VERIFY_DB", "")
# Web-triggered backups run in a background thread so the page returns immediately.
BACKUP_RUN_IN_THREAD = True

# ---------------------------------------------------------------------------
# Email (notifications are optional; console backend by default)
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = env_int("EMAIL_PORT", 25)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "it-helpdesk@hotel.local")

# ---------------------------------------------------------------------------
# Logging: console plus a rotating file in logs/
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_LEVEL = env("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s [%(name)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "hotel_it.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 10,
            "formatter": "standard",
            "encoding": "utf-8",
            # Windows cannot rename a log file that another process holds
            # open, so rotation is deferred until the file is reopened.
            "delay": True,
        },
    },
    "root": {"handlers": ["console", "file"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
    },
}

import sys  # noqa: E402

if len(sys.argv) > 1 and sys.argv[1] == "test":
    # Keep automated test output readable.
    LOGGING["root"]["level"] = "CRITICAL"
    WHITENOISE_AUTOREFRESH = True  # no collectstatic needed for tests
    for _logger in LOGGING["loggers"].values():
        _logger["level"] = "CRITICAL"

# ---------------------------------------------------------------------------
# Application defaults
# ---------------------------------------------------------------------------
HOTEL_NAME_DEFAULT = env("HOTEL_NAME", "Hotel")
# Path of Django's built-in admin site (superusers only). Changing it from
# the default reduces automated probing.
DJANGO_ADMIN_URL = env("DJANGO_ADMIN_URL", "django-admin/")

# Address staff use to reach the system, used for links in notification emails,
# e.g. http://192.168.1.20:8000
SITE_BASE_URL = env("SITE_BASE_URL", "")
