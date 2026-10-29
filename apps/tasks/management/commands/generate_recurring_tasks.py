from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tasks.recurrence import HORIZON_DAYS, generate_all


class Command(BaseCommand):
    help = "Create tasks from active recurring schedules (safe to run repeatedly, e.g. daily from Task Scheduler)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=HORIZON_DAYS, help="How many days ahead to generate.")

    def handle(self, *args, **opts):
        until = timezone.localdate() + timedelta(days=opts["days"])
        created = generate_all(until)
        self.stdout.write(self.style.SUCCESS(f"{created} task(s) generated up to {until:%Y-%m-%d}."))
