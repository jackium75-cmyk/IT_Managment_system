"""Default ticket categories, service targets and escalation rules (configuration, editable by administrators)."""
from django.db import migrations

CATEGORIES = [
    ("Computer / laptop", "Will not start, slow, screen, keyboard, mouse",
     ["Will not start", "Slow or freezing", "Screen / keyboard / mouse", "New computer setup"]),
    ("Printer / scanner", "Printing, scanning, toner, paper jams", ["Not printing", "Paper jam / toner", "Scanning"]),
    ("POS / payment terminal", "Point-of-sale terminals, card readers, receipt printers",
     ["POS error", "Card reader", "Receipt printer"]),
    ("Internet / Wi-Fi", "Staff or guest Wi-Fi, internet outages", ["Guest Wi-Fi", "Staff Wi-Fi", "Internet down"]),
    ("Network / cabling", "Network points, cables, switches", ["Network point / cable", "Switch / router"]),
    ("Hotel applications", "PMS, booking, accounting and other hotel systems",
     ["Property management system (PMS)", "Accounting / back office", "Email", "Other application"]),
    ("Software installation", "New or updated software", []),
    ("Accounts & access", "New accounts, passwords, permissions",
     ["New user account", "Password reset / locked out", "Access permission"]),
    ("Equipment request", "New or replacement equipment", []),
    ("Phones / TV / projector", "Telephones, TVs, projectors, audio-visual", []),
    ("CCTV / security systems", "Cameras, door locks, key-card systems", []),
    ("Other", "Anything else", []),
]

TARGETS = [("critical", 15, 240), ("high", 30, 480), ("medium", 120, 1440), ("low", 480, 4320)]

RULES = [
    dict(name="Critical ticket not picked up in 15 minutes", trigger="unassigned", threshold=15, priorities="critical",
         notify_assignee=True, notify_it_staff=True, notify_admins=True),
    dict(name="Response target missed", trigger="response_breach", threshold=0, priorities="",
         notify_assignee=True, notify_it_staff=True),
    dict(name="75% of resolution time used", trigger="resolution_warning", threshold=75, priorities="critical,high",
         notify_assignee=True),
    dict(name="Resolution target missed", trigger="resolution_breach", threshold=0, priorities="",
         notify_assignee=True, notify_admins=True),
    dict(name="Critical ticket overdue – inform department head", trigger="resolution_breach", threshold=0,
         priorities="critical", notify_department_head=True),
]


def create(apps, schema_editor):
    Category = apps.get_model("helpdesk", "TicketCategory")
    SLATarget = apps.get_model("helpdesk", "SLATarget")
    Rule = apps.get_model("helpdesk", "EscalationRule")
    for order, (name, description, children) in enumerate(CATEGORIES):
        top, _ = Category.objects.get_or_create(name=name, parent=None,
                                                defaults={"description": description, "sort_order": order * 10})
        for i, child in enumerate(children):
            Category.objects.get_or_create(name=child, parent=top, defaults={"sort_order": i * 10})
    for priority, response, resolution in TARGETS:
        SLATarget.objects.get_or_create(priority=priority, defaults={"response_minutes": response,
                                                                     "resolution_minutes": resolution})
    if not Rule.objects.exists():
        for rule in RULES:
            Rule.objects.create(**rule)


class Migration(migrations.Migration):
    dependencies = [("helpdesk", "0001_initial")]
    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
