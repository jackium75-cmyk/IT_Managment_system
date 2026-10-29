"""Default asset categories (configuration; administrators can edit or deactivate them)."""
from django.db import migrations

CATEGORIES = [
    # name, code, maintenance interval (days), expected life (years)
    ("Desktop computer", "PC", 180, 5), ("Laptop", "LAP", 180, 4), ("Printer / scanner", "PRN", 90, 5),
    ("POS terminal", "POS", 90, 5), ("Server", "SRV", 30, 5), ("Network switch", "SW", 180, 7),
    ("Router / firewall", "RTR", 90, 6), ("Wireless access point", "AP", 180, 5), ("UPS", "UPS", 90, 5),
    ("Projector / display", "PRJ", 180, 6), ("Phone / handset", "TEL", None, 6), ("CCTV / security device", "CCTV", 180, 6),
    ("Storage / NAS", "NAS", 90, 5), ("Other equipment", "OTH", None, None),
]


def create(apps, schema_editor):
    Category = apps.get_model("assets", "AssetCategory")
    for name, code, days, life in CATEGORIES:
        Category.objects.get_or_create(code=code, defaults={"name": name, "default_maintenance_days": days,
                                                            "expected_life_years": life})


class Migration(migrations.Migration):
    dependencies = [("assets", "0001_initial")]
    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
