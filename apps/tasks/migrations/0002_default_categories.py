"""Default task categories (configuration, not demo data). Administrators can rename or deactivate them."""
from django.db import migrations

DEFAULTS = [
    ("User support", "support", "Helping staff with IT problems and requests."),
    ("Preventive maintenance", "maintenance", "Scheduled inspection and servicing of equipment."),
    ("Corrective maintenance / repair", "maintenance", "Fixing faulty equipment."),
    ("System check", "system_check", "Connectivity, server, backup and availability checks."),
    ("Backup", "system_check", "Running and reviewing backups."),
    ("Security", "security", "Antivirus, security alerts, access reviews."),
    ("Project work", "project", "Work on IT projects and improvements."),
    ("Administration", "administration", "Reports, procurement paperwork, planning."),
    ("Meeting / consultation", "meeting", "Meetings with management or departments."),
    ("Training", "training", "Training staff or own professional development."),
    ("Documentation", "documentation", "Writing procedures and knowledge-base articles."),
    ("Emergency", "emergency", "Urgent unplanned incidents."),
    ("Other", "other", ""),
]


def create(apps, schema_editor):
    TaskCategory = apps.get_model("tasks", "TaskCategory")
    for name, activity_type, description in DEFAULTS:
        if not TaskCategory.objects.filter(name__iexact=name).exists():
            TaskCategory.objects.create(name=name, activity_type=activity_type, description=description)


class Migration(migrations.Migration):
    dependencies = [("tasks", "0001_initial")]
    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
