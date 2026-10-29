"""
DEVELOPMENT / DEMONSTRATION DATA ONLY.

Creates sample departments, locations and user accounts (all usernames start
with ``demo_``) so the system can be explored. Refuses to run unless
DJANGO_DEBUG is on or ``--allow-production`` is given. Never run this on the
live hotel database.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Department, Location
from apps.accounts.roles import Role

DEPARTMENTS = [
    ("Front Office", "FO"), ("Food & Beverage", "FNB"), ("Housekeeping", "HK"), ("Finance", "FIN"),
    ("Human Resources", "HR"), ("Engineering", "ENG"), ("Sales & Marketing", "SM"), ("Information Technology", "IT"),
]
LOCATIONS = [
    ("Reception Desk", "Main", "Ground", "FO"), ("Restaurant", "Main", "Ground", "FNB"),
    ("Pool Bar", "Pool", "Ground", "FNB"), ("Laundry", "Service", "Basement", "HK"),
    ("Accounts Office", "Main", "1st", "FIN"), ("HR Office", "Main", "1st", "HR"),
    ("Server Room", "Main", "Basement", "IT"), ("IT Office", "Main", "Basement", "IT"),
]
USERS = [
    ("demo_admin", "Amina", "Admin", Role.ADMIN, "IT", "IT Manager"),
    ("demo_it", "Ian", "Tech", Role.IT_OFFICER, "IT", "IT Support Officer"),
    ("demo_fo_manager", "Fatma", "Odhiambo", Role.MANAGER, "FO", "Front Office Manager"),
    ("demo_fnb_manager", "Felix", "Baraka", Role.MANAGER, "FNB", "F&B Manager"),
    ("demo_receptionist", "Rose", "Wanjiru", Role.EMPLOYEE, "FO", "Receptionist"),
    ("demo_cashier", "Chris", "Mwangi", Role.EMPLOYEE, "FNB", "Restaurant Cashier"),
    ("demo_accountant", "Aisha", "Njeri", Role.EMPLOYEE, "FIN", "Accountant"),
]
DEMO_PASSWORD = "Demo-Hotel-2026!"


class Command(BaseCommand):
    help = "Load clearly-labelled DEMO data (departments, locations, demo_* users). Development only."

    def add_arguments(self, parser):
        parser.add_argument("--allow-production", action="store_true",
                            help="Run even when DJANGO_DEBUG is off (e.g. a training server).")

    @transaction.atomic
    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["allow_production"]:
            raise CommandError("Demo data is only loaded when DJANGO_DEBUG=1 (or with --allow-production).")
        User = get_user_model()
        depts = {}
        for name, code in DEPARTMENTS:
            depts[code], _ = Department.objects.get_or_create(code=code, defaults={"name": name})
        for name, building, floor, code in LOCATIONS:
            Location.objects.get_or_create(name=name, building=building, floor=floor,
                                           defaults={"department": depts[code]})
        for username, first, last, role, code, title in USERS:
            user, created = User.objects.get_or_create(
                username=username, defaults={"first_name": first, "last_name": last,
                                             "email": f"{username}@example.invalid"})
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
            p = user.profile
            p.role, p.department, p.job_title = role, depts[code], title
            p.save()
            if role == Role.MANAGER and depts[code].head_id is None:
                depts[code].head = user
                depts[code].save(update_fields=["head"])
        self._seed_tasks(User)
        self._seed_assets(User)
        self._seed_tickets(User)
        self._seed_phase5(User)
        self.stdout.write(self.style.WARNING("DEMO DATA loaded. Demo accounts (password: %s):" % DEMO_PASSWORD))
        for username, *_rest in USERS:
            self.stdout.write(f"  {username}")

    def _seed_tasks(self, User):
        """Phase 2 demo: recurring checks, a day of tasks, time entries, an interruption, a manual entry."""
        from datetime import datetime, time, timedelta
        from unittest import mock

        from django.utils import timezone

        from apps.diary.choices import ActivityType
        from apps.diary.models import ActivityLog
        from apps.tasks import services
        from apps.tasks.models import RecurringTask, Task, TaskCategory
        from apps.tasks.recurrence import generate_for

        it = User.objects.get(username="demo_it")
        if Task.objects.filter(assigned_to=it).exists():
            return  # already seeded
        cat = {c.name: c for c in TaskCategory.objects.all()}
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        fo = Department.objects.get(code="FO")

        def at(day, hh, mm=0):
            return timezone.make_aware(datetime.combine(day, time(hh, mm)))

        for title, category, freq, extra in [
            ("[DEMO] Check hotel internet connectivity", "System check", "daily",
             {"skip_weekends": False, "scheduled_start": time(8, 0), "scheduled_end": time(8, 15), "estimated_minutes": 15}),
            ("[DEMO] Review backups", "Backup", "weekly", {"weekdays": "0,3", "scheduled_start": time(9, 0), "estimated_minutes": 20}),
            ("[DEMO] Inspect server health", "Preventive maintenance", "monthly", {"day_of_month": 1, "estimated_minutes": 45}),
        ]:
            rt = RecurringTask.objects.create(title=title, category=cat[category], frequency=freq, assigned_to=it,
                                              start_date=yesterday, created_by=it, **extra)
            RecurringTask.objects.filter(pk=rt.pk).update(created_at=at(yesterday, 7))
            rt.refresh_from_db()
            generate_for(rt)

        def as_of(moment):
            """Record a demo action as if it happened at ``moment`` (demo data only)."""
            return mock.patch("django.utils.timezone.now", return_value=moment)

        def new(title, category, day, start=None, end=None, **kw):
            return services.create_task(Task(title=title, category=cat[category], planned_date=day,
                                             scheduled_start=start, scheduled_end=end, assigned_to=it, **kw), it)

        done = new("[DEMO] Configure new receptionist PC", "User support", yesterday, time(10), time(11),
                   department=fo, estimated_minutes=60, expected_outcome="PC joined to domain with PMS access.")
        services.log_time(done, it, started_at=at(yesterday, 10, 5), minutes=55, note="Imaged and joined domain")
        with as_of(at(yesterday, 11, 5)):
            services.change_status(done, Task.Status.COMPLETED, it, completion_notes="PC installed, PMS and printer tested with user.")
        moved = new("[DEMO] Replace toner in back-office printer", "Corrective maintenance / repair", yesterday, time(14))
        with as_of(at(yesterday, 16, 30)):
            services.reschedule(moved, it, new_date=today, new_start=time(14), reason="Toner delivery arrives today.")
        waiting = new("[DEMO] Repair POS terminal 2 card reader", "Corrective maintenance / repair", today, time(11), time(12),
                      priority="high", estimated_minutes=60)
        services.change_status(waiting, Task.Status.WAITING_PARTS, it, reason="Replacement card reader ordered from vendor.")
        new("[DEMO] Update antivirus definitions on all PCs", "Security", today, estimated_minutes=30)
        new("[DEMO] Prepare monthly IT report", "Administration", today + timedelta(days=1), time(15), time(16))

        urgent = Task(title="[DEMO] Wi-Fi down in conference room", category=cat["Emergency"], priority="critical",
                      assigned_to=it, description="Event starting in 30 minutes; access point unresponsive.")
        with as_of(at(yesterday, 11, 58)):
            services.record_unplanned(urgent, it, worked_from=at(yesterday, 11, 15), worked_minutes=40,
                                      completion_notes="Power-cycled PoE switch port; AP back online, tested with organiser.")
        ActivityLog.objects.create(
            user=it, created_by=it, source=ActivityLog.Source.MANUAL, event=ActivityLog.Event.MANUAL,
            activity_type=ActivityType.MEETING, occurred_at=at(yesterday, 15), duration_minutes=45,
            title="[DEMO] Meeting with Front Office manager about PMS access", department=fo,
            outcome="Agreed list of new user accounts needed next week.")

    def _seed_tickets(self, User):
        """Phase 3 demo: tickets in different states, evidence, a linked task, notifications."""
        import io
        from datetime import timedelta
        from unittest import mock

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils import timezone
        from PIL import Image, ImageDraw

        from apps.assets.models import Asset
        from apps.helpdesk import services as hd
        from apps.helpdesk.models import Ticket, TicketCategory
        from apps.tasks import services as ts
        from apps.tasks.models import Task, TaskCategory

        if Ticket.objects.filter(subject__startswith="[DEMO]").exists():
            return
        it = User.objects.get(username="demo_it")
        rose = User.objects.get(username="demo_receptionist")
        chris = User.objects.get(username="demo_cashier")
        aisha = User.objects.get(username="demo_accountant")
        cat = lambda name: TicketCategory.objects.get(name=name, parent=None)  # noqa: E731
        now = timezone.now()

        def photo(text, colour):
            img = Image.new("RGB", (480, 320), colour)
            ImageDraw.Draw(img).text((20, 140), text, fill="white")
            buf = io.BytesIO()
            img.save(buf, "JPEG")
            f = SimpleUploadedFile(f"{text.lower().replace(' ', '-')}.jpg", buf.getvalue(), content_type="image/jpeg")
            return [(f, "jpg", "image", "image/jpeg")]

        def at(minutes_ago):
            return mock.patch("django.utils.timezone.now", return_value=now - timedelta(minutes=minutes_ago))

        with at(60 * 26):
            printer = hd.create_ticket(Ticket(
                asset=Asset.objects.filter(category__code="PRN").first(),
                subject="[DEMO] Reception printer shows error E-05", category=cat("Printer / scanner"),
                description="The printer at the front desk stopped printing guest folios. Display shows E-05.",
                impact="several", urgency="slowed", affected_device="Reception printer", location_detail="Front desk"),
                rose, photo("ERROR E-05", "darkred"))
        with at(60 * 25):
            hd.change_status(printer, Ticket.Status.ACKNOWLEDGED, it)
            hd.add_comment(printer, it, "Fuser unit reports overheating; let it cool and cleaned the paper path.",
                           kind="troubleshooting")
            hd.log_time(printer, it, started_at=timezone.now() - timedelta(minutes=40), minutes=35)
        with at(60 * 23):
            hd.change_status(printer, Ticket.Status.RESOLVED, it, resolution_details="Cleared paper jam behind the fuser and "
                             "replaced worn pickup roller. Printed test page and a guest folio.",
                             root_cause="Worn pickup roller caused repeated jams.", verification="Printed 20 pages without error.")
            hd.add_comment(printer, it, "Printer working again – photo attached.", uploads=photo("PRINTER OK", "darkgreen"),
                           stage="after_fix")
        with at(60 * 20):
            hd.confirm_resolution(printer, rose, rating=5, comment="Thank you, very quick!")

        with at(90):
            pos = hd.create_ticket(Ticket(
                asset=Asset.objects.filter(category__code="POS").first(),
                subject="[DEMO] POS terminal 2 card reader not reading cards", category=cat("POS / payment terminal"),
                description="Guests' cards are declined at the restaurant till 2; cash only right now.",
                impact="guests", urgency="stopped", affected_device="POS 2", location_detail="Restaurant till"), chris)
        with at(80):
            hd.start_timer(pos, it)
        from apps.diary import timekeeping
        with at(50):
            timekeeping.stop_running(it, "Swapped cable; reader still faulty")
        with at(45):
            hd.change_status(pos, Ticket.Status.WAITING, it, waiting_on="parts",
                             note="Replacement card reader ordered from the POS vendor; till 1 in use meanwhile.")
        task = Task.objects.filter(title__startswith="[DEMO] Repair POS terminal 2").first()
        if task:
            ts.link_to_ticket(task, pos, it)

        with at(200):
            access = hd.create_ticket(Ticket(
                ticket_type="request", subject="[DEMO] Access to the accounting shared folder",
                category=cat("Accounts & access"), description="I need read access to the month-end folder.",
                impact="one", urgency="minor"), aisha)
        with at(30):
            hd.change_status(access, Ticket.Status.RESOLVED, it, resolution_details="Added Aisha to the Finance-Read group.")

        hd.create_ticket(Ticket(subject="[DEMO] Guest Wi-Fi slow in the conference room", category=cat("Internet / Wi-Fi"),
                                description="Event organiser reports very slow Wi-Fi during presentations.",
                                impact="guests", urgency="slowed", location_detail="Conference room"), rose)
        ts.create_task(Task(title="[DEMO] Survey Wi-Fi coverage in conference room",
                            category=TaskCategory.objects.get(name="User support"),
                            planned_date=timezone.localdate() + timedelta(days=1), assigned_to=it), it)

    def _seed_assets(self, User):
        """Phase 4 demo: suppliers, assets with history, maintenance plans in different states."""
        from datetime import date, timedelta
        from decimal import Decimal
        from unittest import mock

        from django.utils import timezone

        from apps.assets import services as asvc
        from apps.assets.models import Asset, AssetCategory, Supplier
        from apps.maintenance import services as msvc
        from apps.maintenance.models import MaintenancePlan

        if Asset.objects.filter(notes__startswith="[DEMO]").exists():
            return
        it = User.objects.get(username="demo_it")
        rose = User.objects.get(username="demo_receptionist")
        today = timezone.localdate()
        dept = {d.code: d for d in Department.objects.all()}
        loc = {l.name: l for l in Location.objects.all()}
        cat = {c.code: c for c in AssetCategory.objects.all()}
        supplier, _ = Supplier.objects.get_or_create(name="[DEMO] City Office Systems",
                                                     defaults={"phone": "+000 000 000", "contact_person": "Sales desk"})
        specs = [
            ("PRN", "Reception printer", "HP", "LaserJet M404dn", "FO", "Reception Desk", None, date(2021, 3, 10), "420", 1095, "good"),
            ("PC", "Reception PC 1", "Dell", "OptiPlex 7090", "FO", "Reception Desk", rose, date(2022, 1, 5), "850", 1095, "good"),
            ("POS", "Restaurant POS terminal 2", "Verifone", "V400m", "FNB", "Restaurant", None, date(2019, 6, 1), "650", 730, "poor"),
            ("SRV", "PMS application server", "Dell", "PowerEdge R450", "IT", "Server Room", None, date(2020, 9, 1), "4800", 1825, "good"),
            ("SW", "Core network switch", "Cisco", "Catalyst 9200", "IT", "Server Room", None, date(2018, 2, 1), "2100", 365, "fair"),
            ("AP", "Conference room access point", "Ubiquiti", "U6 Pro", "SM", None, None, date(2023, 4, 1), "190", 730, "good"),
            ("UPS", "Server room UPS", "APC", "Smart-UPS 3000", "IT", "Server Room", None, date(2017, 5, 1), "1500", 365, "fair"),
        ]
        assets = {}
        for code, name, maker, model, d, l, user, bought, cost, warranty_days, condition in specs:
            a = Asset(name=name, category=cat[code], manufacturer=maker, model=model, department=dept.get(d),
                      location=loc.get(l) if l else None, assigned_to=user, purchase_date=bought,
                      purchase_cost=Decimal(cost), supplier=supplier, condition=condition,
                      warranty_expiry=bought + timedelta(days=warranty_days),
                      serial_number=f"DEMO-{code}-{len(assets) + 1:03d}", notes="[DEMO] sample asset")
            if code in ("PC", "SRV"):
                a.hostname, a.operating_system = f"{code.lower()}-demo-{len(assets) + 1:02d}", "Windows Server 2022" if code == "SRV" else "Windows 11"
            assets[code] = asvc.create_asset(a, it)
        assets["POS"].replacement = "recommended"
        assets["POS"].replacement_reason = "Repeated card-reader faults; model out of vendor support."
        assets["POS"].save()
        assets["AP"].warranty_expiry = today + timedelta(days=25)
        assets["AP"].save()

        def plan(asset, title, value, unit, due, checklist):
            return msvc.create_plan(MaintenancePlan(asset=asset, title=title, interval_value=value, interval_unit=unit,
                                                    next_due_date=due, lead_days=7, responsible=it, estimated_minutes=45,
                                                    checklist=checklist), it)

        srv = plan(assets["SRV"], "Monthly server health check", 1, "months", today - timedelta(days=20),
                   "Check RAID / disk health\nReview event logs\nVerify last backup restored\nApply security updates")
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() - timedelta(days=19)):
            rec = srv.records.get(status="scheduled")
            msvc.complete_record(rec, it, completed_on=today - timedelta(days=19), work_done="Health check completed; updates applied.",
                                 checklist_results=[{"item": i, "result": "ok", "note": ""} for i in srv.checklist_items])
        plan(assets["PRN"], "Quarterly printer service", 3, "months", today + timedelta(days=4),
             "Clean paper path and rollers\nCheck fuser\nPrint test page")
        plan(assets["UPS"], "UPS battery and load test", 3, "months", today - timedelta(days=3),
             "Run self-test\nCheck battery age\nCheck load percentage")
        plan(assets["SW"], "Switch firmware and port review", 6, "months", today + timedelta(days=40),
             "Check firmware version\nReview error counters\nBack up configuration")
        msvc.record_corrective(assets["POS"], it, title="Replaced card-reader cable", completed_on=today - timedelta(days=30),
                               work_done="Swapped damaged reader cable; tested chip and contactless.",
                               problems_found="Cable insulation worn through", parts_used="1 × reader cable",
                               cost=Decimal("35.00"))
        msvc.run_jobs(today)

    def _seed_phase5(self, User):
        """Phase 5 demo: a project with milestones, procurement in several states, knowledge articles."""
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from apps.assets.models import Asset, AssetCategory, Supplier
        from apps.helpdesk.models import Ticket, TicketCategory
        from apps.knowledge import services as kb
        from apps.knowledge.models import KnowledgeArticle
        from apps.procurement import services as proc
        from apps.procurement.models import ProcurementRequest
        from apps.projects import services as ps
        from apps.projects.models import Project, ProjectMilestone
        from apps.tasks.models import Task

        if Project.objects.filter(name__startswith="[DEMO]").exists():
            return
        it = User.objects.get(username="demo_it")
        admin = User.objects.get(username="demo_admin")
        today = timezone.localdate()
        dept = {d.code: d for d in Department.objects.all()}
        p = ps.create_project(Project(
            name="[DEMO] Conference & guest Wi-Fi upgrade", description="Replace ageing access points and re-plan channels.",
            business_objective="Reduce guest Wi-Fi complaints and support events in the conference room.",
            status="in_progress", priority="high", owner=it, sponsor_department=dept["SM"],
            start_date=today - timedelta(days=20), target_date=today + timedelta(days=25), estimated_cost=Decimal("2500"),
            risks="Supplier lead time for APs is 3 weeks."), it)
        for i, (title, due, done) in enumerate([("Coverage survey", -14, True), ("Order access points", -5, True),
                                                ("Install and configure", 10, False), ("Sign-off with Sales", 20, False)]):
            m = ps.save_milestone(ProjectMilestone(project=p, title=title, due_date=today + timedelta(days=due), order=i), it)
            if done:
                ps.toggle_milestone(m, it)
        ps.add_update(p, it, "Survey found three dead zones; APs ordered from supplier.")
        Task.objects.filter(title__startswith="[DEMO] Survey Wi-Fi").update(project=p)
        Ticket.objects.filter(subject__startswith="[DEMO] Guest Wi-Fi slow").update(project=p)

        supplier = Supplier.objects.filter(name__startswith="[DEMO]").first()
        ap = proc.create_request(ProcurementRequest(
            item="[DEMO] 4 × Wi-Fi 6 access points", quantity=4, reason="Replace failing conference room APs (project).",
            estimated_cost=Decimal("760"), department=dept["SM"], project=p, supplier=supplier), it)
        proc.submit(ap, it)
        proc.decide(ap, admin, "approved")
        proc.mark_ordered(ap, it)
        pos = Asset.objects.filter(category__code="POS").first()
        reader = proc.create_request(ProcurementRequest(
            request_type="repair", item="[DEMO] Replacement card reader for POS 2", reason="Card reader fault (see ticket).",
            estimated_cost=Decimal("140"), department=dept["FNB"], asset=pos,
            ticket=Ticket.objects.filter(subject__startswith="[DEMO] POS").first(), supplier=supplier), it)
        proc.submit(reader, it)  # left awaiting approval by the F&B manager / admin
        toner = proc.create_request(ProcurementRequest(
            item="[DEMO] Toner cartridges (×3)", quantity=3, reason="Stock for reception printer.",
            estimated_cost=Decimal("90"), department=dept["FO"],
            asset=Asset.objects.filter(category__code="PRN").first()), it)
        proc.submit(toner, it)
        proc.decide(toner, User.objects.get(username="demo_fo_manager"), "approved")
        proc.mark_delivered(toner, it, actual_expenditure=Decimal("84"), delivery_date=today - timedelta(days=2),
                            outcome="Received and stored in IT office.")

        cat = lambda n: TicketCategory.objects.filter(name=n, parent=None).first()  # noqa: E731
        dev = lambda c: AssetCategory.objects.filter(code=c).first()  # noqa: E731
        articles = [
            dict(title="Printer shows E-05 and stops printing", category=cat("Printer / scanner"), asset_category=dev("PRN"),
                 error_codes="E-05", problem="Printer stops and shows E-05; paper jam light on.",
                 symptoms="Paper jam light, partly fed sheet near the fuser.", possible_causes="Worn pickup roller; debris in paper path.",
                 diagnostics="Open rear door, check fuser area, print test page.", root_cause="Worn pickup roller.",
                 solution="Clear the paper path, clean the rollers and replace the pickup roller if worn.",
                 verification="Print 20 pages without a jam.", prevention="Quarterly printer service plan.", verified=True),
            dict(title="POS card reader declines all cards", category=cat("POS / payment terminal"), asset_category=dev("POS"),
                 error_codes="CARD READ ERROR", problem="All cards declined at one till with 'card read error'.",
                 diagnostics="Swap reader cable; try another reader on the same till.", root_cause="Damaged reader cable or reader.",
                 solution="Replace the reader cable; if still failing, replace the card reader. Use another till meanwhile.",
                 verified=False),
            dict(title="User locked out after password change", category=cat("Accounts & access"),
                 problem="User cannot log in after changing password on another device.",
                 possible_causes="Old password cached on phone mail app causing lockouts.",
                 solution="Unlock the account, then update the saved password on the user's phone and other devices.",
                 verified=True),
        ]
        for a in articles:
            verified = a.pop("verified")
            art = kb.save_article(KnowledgeArticle(status="published", **a), it, created=True)
            if verified:
                kb.set_verified(art, it, True)
