from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dashboard"
    verbose_name = "Dashboard"

    def ready(self):
        from . import providers  # noqa: F401  (registers Phase 1 metrics)
