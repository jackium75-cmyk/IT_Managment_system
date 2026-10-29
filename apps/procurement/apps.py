from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.procurement"
    verbose_name = "Procurement"

    def ready(self):
        from . import dashboard  # noqa: F401  (registers dashboard metrics)
