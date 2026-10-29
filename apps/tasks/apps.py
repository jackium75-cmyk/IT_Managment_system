from django.apps import AppConfig


class TasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tasks"
    verbose_name = "Tasks and planner"

    def ready(self):
        from . import dashboard  # noqa: F401  (registers dashboard metrics)
