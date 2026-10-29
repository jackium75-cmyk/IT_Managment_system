from django.core.management.base import BaseCommand

from apps.core.jobs import run_all


class Command(BaseCommand):
    help = "Alias of run_jobs (kept so existing scheduled tasks keep working)."

    def handle(self, *args, **opts):
        self.stdout.write(self.style.SUCCESS(f"Jobs completed: {run_all()}"))
