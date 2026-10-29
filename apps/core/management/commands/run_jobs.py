from django.core.management.base import BaseCommand

from apps.core.jobs import run_all


class Command(BaseCommand):
    help = ("Run all scheduled jobs: recurring tasks, maintenance tasks/reminders/missed visits, ticket escalations, "
            "auto-close and overdue-task reminders. Safe to run repeatedly; schedule every 10 minutes.")

    def handle(self, *args, **opts):
        result = run_all()
        self.stdout.write(self.style.SUCCESS(f"Jobs completed: {result}"))
