"""Phase 5: projects, procurement & approvals, knowledge base, global search."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.assets import services as asset_services
from apps.assets.models import Asset, AssetCategory
from apps.core.models import AuditLog, SystemSetting
from apps.core.testing import HotelTestCase
from apps.helpdesk import services as hd
from apps.helpdesk.models import Ticket, TicketCategory, TicketComment
from apps.knowledge import matching
from apps.knowledge import services as kb
from apps.knowledge.models import KnowledgeArticle
from apps.notifications.models import Notification
from apps.procurement import services as proc
from apps.procurement.models import Approval, ProcurementRequest
from apps.tasks import services as task_services
from apps.tasks.models import Task, TaskCategory

from . import services
from .models import Project, ProjectMilestone

PS = ProcurementRequest.Status


class Phase5TestCase(HotelTestCase):
    def setUp(self):
        super().setUp()
        self.it = self.make_user("ian", Role.IT_OFFICER)
        self.it2 = self.make_user("ivy", Role.IT_OFFICER)
        self.admin = self.make_user("adm", Role.ADMIN)
        self.mgr = self.make_user("fatma", Role.MANAGER)
        self.mgr_other = self.make_user("felix", Role.MANAGER, department=self.other_dept)
        self.emp = self.make_user("rose")
        self.dept.head = self.mgr
        self.dept.save()
        self.today = timezone.localdate()

    def project(self, **kw):
        kw.setdefault("name", "Guest Wi-Fi upgrade")
        kw.setdefault("description", "Replace access points")
        kw.setdefault("business_objective", "Fewer guest complaints")
        kw.setdefault("owner", self.it)
        kw.setdefault("sponsor_department", self.dept)
        return services.create_project(Project(**kw), self.it)

    def request(self, **kw):
        kw.setdefault("item", "Replacement access point")
        kw.setdefault("reason", "Conference room AP failing")
        kw.setdefault("estimated_cost", Decimal("190"))
        kw.setdefault("department", self.dept)
        return proc.create_request(ProcurementRequest(**kw), self.it)


class ProjectTests(Phase5TestCase):
    def test_codes_status_and_completion_rule(self):
        p = self.project()
        self.assertTrue(p.code.startswith(f"PRJ-{self.today.year}-"))
        before = Project.objects.get(pk=p.pk)
        p.status = Project.Status.COMPLETED
        with self.assertRaises(ValidationError):
            services.update_project(p, before, self.it)  # final outcome required
        p.final_outcome = "All 12 APs replaced"
        services.update_project(p, before, self.it)
        p.refresh_from_db()
        self.assertEqual(p.completed_on, self.today)
        self.assertTrue(p.updates.filter(is_system=True, body__contains="Completed").exists())

    def test_savings_require_evidence(self):
        with self.assertRaises(ValidationError):
            self.project(documented_savings=Decimal("500"), estimated_cost=Decimal("1000"), currency="USD")
        p = self.project(documented_savings=Decimal("500"), savings_evidence="ISP invoice comparison Jan vs Mar",
                         estimated_cost=Decimal("1000"))
        self.assertEqual(p.currency, SystemSetting.get().currency)

    def test_milestones_progress_and_log(self):
        p = self.project()
        a = services.save_milestone(ProjectMilestone(project=p, title="Survey"), self.it)
        services.save_milestone(ProjectMilestone(project=p, title="Install"), self.it)
        self.assertEqual(p.progress, 0)
        services.toggle_milestone(a, self.it)
        self.assertEqual(p.progress, 50)
        services.add_update(p, self.it, "Survey done; 3 dead zones found")
        self.assertTrue(p.activities.filter(title__contains="Survey done").exists())  # diary entry

    def test_time_and_costs_roll_up_once(self):
        p = self.project(estimated_cost=Decimal("2000"), other_costs=Decimal("50"))
        cat = TaskCategory.objects.get(name="Project work")
        task = task_services.create_task(Task(title="Install APs", category=cat, planned_date=self.today,
                                              assigned_to=self.it, project=p), self.it)
        start = timezone.make_aware(datetime.combine(self.today - timedelta(days=1), time(9)))
        task_services.log_time(task, self.it, started_at=start, minutes=90)
        ticket = hd.create_ticket(Ticket(subject="AP offline", description="x", project=p,
                                         category=TicketCategory.objects.get(name="Internet / Wi-Fi", parent=None)), self.emp)
        hd.log_time(ticket, self.it, started_at=start + timedelta(hours=2), minutes=30)
        task_services.link_to_ticket(task, ticket, self.it)  # moved, not duplicated
        self.assertEqual(services.time_minutes(p), 120)
        r = self.request(project=p, estimated_cost=Decimal("1500"))
        proc.submit(r, self.it)
        proc.decide(r, self.mgr, "approved")
        proc.mark_delivered(r, self.it, actual_expenditure=Decimal("1450"), delivery_date=self.today)
        c = services.costs(p)[SystemSetting.get().currency]
        self.assertEqual((c["estimated"], c["approved"], c["actual"]), (Decimal("2000"), Decimal("1500"), Decimal("1500")))

    def test_visibility(self):
        p = self.project()
        other = self.project(name="F&B tablets", sponsor_department=self.other_dept)
        self.login(self.mgr)
        self.assertEqual(self.client.get(p.get_absolute_url()).status_code, 200)
        self.assertEqual(self.client.get(other.get_absolute_url()).status_code, 403)
        self.assertEqual(self.client.get(reverse("projects:edit", args=[p.pk])).status_code, 403)
        self.login(self.emp)
        self.assertEqual(self.client.get(reverse("projects:list")).status_code, 403)

    def test_report_states_what_is_and_is_not_verified(self):
        p = self.project(benefits="Fewer complaints reported by Front Office")
        self.login(self.it)
        resp = self.client.get(reverse("projects:report", args=[p.pk]))
        self.assertContains(resp, "not independently verified")
        self.assertContains(resp, "No cost savings are claimed")


class ProcurementTests(Phase5TestCase):
    def test_approval_by_department_head_never_by_requester(self):
        r = self.request()
        self.assertEqual(r.status, PS.DRAFT)
        r = proc.submit(r, self.it)
        self.assertEqual(r.status, PS.PENDING)
        self.assertTrue(Notification.objects.filter(recipient=self.mgr, kind=Notification.Kind.APPROVAL).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.it, kind=Notification.Kind.APPROVAL).exists())
        with self.assertRaises(PermissionDenied):
            proc.decide(r, self.it, "approved")       # requester
        with self.assertRaises(PermissionDenied):
            proc.decide(r, self.mgr_other, "approved")  # another department's head
        with self.assertRaises(ValidationError):
            proc.decide(r, self.mgr, "rejected", comment="")  # reason needed
        r = proc.decide(r, self.mgr, "approved", approved_amount=Decimal("180"))
        self.assertEqual((r.status, r.approved_cost), (PS.APPROVED, Decimal("180")))
        self.assertEqual(Approval.objects.get(request=r).approver, self.mgr)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.APPROVAL, object_id=str(r.pk)).exists())

    def test_rejection_and_resubmission_keep_history(self):
        r = proc.submit(self.request(), self.it)
        proc.decide(r, self.admin, "rejected", comment="Get a second quotation")
        r.refresh_from_db()
        before = ProcurementRequest.objects.get(pk=r.pk)
        r.estimated_cost = Decimal("150")
        proc.update_request(r, before, self.it)  # editable again after rejection
        proc.submit(r, self.it)
        proc.decide(r, self.admin, "approved")
        self.assertEqual(r.approvals.count(), 2)

    def test_amounts_locked_after_submission(self):
        r = proc.submit(self.request(), self.it)
        before = ProcurementRequest.objects.get(pk=r.pk)
        r.estimated_cost = Decimal("9999")
        with self.assertRaises(ValidationError):
            proc.update_request(r, before, self.it)

    def test_auto_approval_below_threshold(self):
        cfg = SystemSetting.get()
        cfg.procurement_approval_threshold = Decimal("200")
        cfg.save()
        small = proc.submit(self.request(estimated_cost=Decimal("50")), self.it)
        big = proc.submit(self.request(estimated_cost=Decimal("500")), self.it)
        self.assertEqual((small.status, small.approved_cost), (PS.APPROVED, Decimal("50")))
        self.assertEqual(big.status, PS.PENDING)

    def test_order_delivery_and_asset_history(self):
        asset = asset_services.create_asset(Asset(name="Conf AP", category=AssetCategory.objects.get(code="AP")), self.it)
        r = proc.submit(self.request(asset=asset), self.it)
        with self.assertRaises(ValidationError):
            proc.mark_ordered(r, self.it)  # not approved yet
        proc.decide(r, self.mgr, "approved")
        proc.mark_ordered(ProcurementRequest.objects.get(pk=r.pk), self.it)
        r = proc.mark_delivered(ProcurementRequest.objects.get(pk=r.pk), self.it, actual_expenditure=Decimal("185"),
                                delivery_date=self.today, outcome="Installed")
        self.assertEqual(r.variance, Decimal("-5"))
        cur = SystemSetting.get().currency
        self.assertEqual(asset_services.expenses(asset), {cur: Decimal("185")})
        self.assertIn("purchase", {e["kind"] for e in asset_services.service_history(asset)})

    def test_manager_views_and_approves_via_ui(self):
        r = proc.submit(self.request(), self.it)
        other = proc.submit(self.request(department=self.other_dept), self.it)
        self.login(self.mgr)
        resp = self.client.get(reverse("procurement:list"), {"view": "approval"})
        self.assertContains(resp, r.reference)
        self.assertNotContains(resp, other.reference)
        self.assertEqual(self.client.get(other.get_absolute_url()).status_code, 403)
        self.client.post(reverse("procurement:decide", args=[r.pk]), {"decision": "approved"})
        r.refresh_from_db()
        self.assertEqual(r.status, PS.APPROVED)
        self.assertEqual(self.client.get(reverse("procurement:create")).status_code, 403)  # only IT raises requests
        self.login(self.emp)
        self.assertEqual(self.client.get(reverse("procurement:list")).status_code, 403)


class KnowledgeTests(Phase5TestCase):
    def setUp(self):
        super().setUp()
        self.printers = TicketCategory.objects.get(name="Printer / scanner", parent=None)
        self.prn = AssetCategory.objects.get(code="PRN")
        self.article = kb.save_article(KnowledgeArticle(
            title="Printer shows E-05 and stops printing", status="published", category=self.printers,
            asset_category=self.prn, error_codes="E-05", problem="Printer stops with error E-05",
            symptoms="Paper jam light, fuser hot", solution="Clean the fuser path and replace pickup roller"), self.it,
            created=True)
        kb.save_article(KnowledgeArticle(title="Wi-Fi slow in conference room", status="published",
                                         problem="Slow Wi-Fi", solution="Move channel"), self.it, created=True)

    def ticket(self, **kw):
        kw.setdefault("category", self.printers)
        return hd.create_ticket(Ticket(**kw), self.emp)

    def test_error_code_and_device_matching_with_reasons(self):
        asset = asset_services.create_asset(Asset(name="Reception printer", category=self.prn), self.it)
        t = self.ticket(subject="Reception printer error e05", description="It says E05 and nothing prints", asset=asset)
        results = matching.suggest(t)
        self.assertEqual(results[0][1], self.article)
        reasons = " ".join(results[0][2])
        self.assertIn("E05", reasons)
        self.assertIn("device type", reasons)
        self.assertNotIn("Wi-Fi", [r[1].title for r in results])

    def test_weak_matches_are_not_suggested(self):
        t = self.ticket(subject="Need a new mouse", description="Mine is worn",
                        category=TicketCategory.objects.get(name="Equipment request", parent=None))
        self.assertEqual(matching.suggest(t), [])

    def test_drafts_never_suggested(self):
        self.article.status = "draft"
        self.article.save()
        t = self.ticket(subject="E-05 error", description="E-05")
        self.assertEqual(matching.suggest(t), [])

    def test_article_from_ticket_and_apply(self):
        t = self.ticket(subject="Printer E-05", description="E-05 again")
        hd.add_comment(t, self.it, "Checked fuser temperature", kind=TicketComment.Kind.TROUBLESHOOTING)
        t = hd.change_status(t, Ticket.Status.RESOLVED, self.it, resolution_details="Replaced roller",
                             root_cause="Worn roller")
        draft = kb.from_ticket(t)
        self.assertIn("Checked fuser temperature", draft.diagnostics)
        self.assertEqual((draft.solution, draft.root_cause), ("Replaced roller", "Worn roller"))
        t2 = self.ticket(subject="E-05 on back office printer", description="E-05")
        kb.apply_to_ticket(self.article, t2, self.it)
        self.assertIn(t2, self.article.used_in_tickets.all())
        self.assertTrue(t2.comments.filter(kind="internal", body__contains="Applied knowledge article").exists())

    def test_secrets_refused_and_verification(self):
        with self.assertRaises(ValidationError):
            kb.save_article(KnowledgeArticle(title="Router", problem="x", solution="Login with password: admin123"),
                            self.it, created=True)
        kb.set_verified(self.article, self.it, True)
        self.article.refresh_from_db()
        self.assertTrue(self.article.is_verified)
        self.assertEqual(self.article.verified_by, self.it)

    def test_ticket_page_shows_suggestions_to_it_only(self):
        t = self.ticket(subject="Printer E-05", description="E-05")
        self.login(self.it)
        self.assertContains(self.client.get(t.get_absolute_url()), self.article.title)
        self.login(self.emp)
        self.assertNotContains(self.client.get(t.get_absolute_url()), "Possibly related solutions")
        self.assertEqual(self.client.get(reverse("knowledge:list")).status_code, 403)

    def test_search_ranks_codes(self):
        found = matching.search("e05", KnowledgeArticle.objects.all())
        self.assertEqual(found[0], self.article)


class SearchAndPagesTests(Phase5TestCase):
    def test_global_search_respects_roles(self):
        mine = hd.create_ticket(Ticket(subject="Keyboard sticky", description="x",
                                       category=TicketCategory.objects.get(name="Other", parent=None)), self.emp)
        theirs = hd.create_ticket(Ticket(subject="Keyboard broken", description="x",
                                         category=TicketCategory.objects.get(name="Other", parent=None)),
                                  self.make_user("chris", department=self.other_dept))
        asset_services.create_asset(Asset(name="Keyboard spare", category=AssetCategory.objects.get(code="OTH")), self.it)
        self.login(self.emp)
        resp = self.client.get(reverse("core:search"), {"q": "keyboard"})
        self.assertContains(resp, mine.reference)
        self.assertNotContains(resp, theirs.reference)
        self.assertNotContains(resp, "Keyboard spare")
        self.login(self.it)
        resp = self.client.get(reverse("core:search"), {"q": "keyboard"})
        self.assertContains(resp, theirs.reference)
        self.assertContains(resp, "Keyboard spare")

    def test_pages_render(self):
        p = self.project()
        r = self.request(project=p)
        a = kb.save_article(KnowledgeArticle(title="T", status="published", problem="p", solution="s"), self.it, created=True)
        self.login(self.it)
        for url in (reverse("projects:list"), reverse("projects:create"), p.get_absolute_url(),
                    reverse("projects:edit", args=[p.pk]), reverse("projects:report", args=[p.pk]),
                    reverse("procurement:list"), reverse("procurement:list") + "?view=all", reverse("procurement:create"),
                    r.get_absolute_url(), reverse("procurement:edit", args=[r.pk]), reverse("knowledge:list"),
                    reverse("knowledge:list") + "?q=printer", reverse("knowledge:create"), a.get_absolute_url(),
                    reverse("knowledge:edit", args=[a.pk]), reverse("core:search") + "?q=wi", reverse("dashboard:index"),
                    reverse("tasks:create") + f"?project={p.pk}", reverse("diary:list") + f"?project={p.code}"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        proc.submit(r, self.it)
        proc.decide(r, self.admin, "approved")
        for url in (reverse("procurement:ordered", args=[r.pk]), reverse("procurement:delivered", args=[r.pk]),
                    reverse("procurement:cancel", args=[r.pk])):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.login(self.mgr)
        resp = self.client.get(reverse("dashboard:index"))
        self.assertIn("Projects & approvals", resp.context["card_sections"])


class BenefitConfirmationTests(Phase5TestCase):
    """Verified benefits must come from the sponsoring department's manager personally, never from IT."""

    def completed(self, **kw):
        project = self.project(benefits="Guest Wi-Fi complaints fell from 30 to 4 a month (guest survey).", **kw)
        before = Project.objects.get(pk=project.pk)
        project.status, project.final_outcome = Project.Status.COMPLETED, "All access points replaced."
        return services.update_project(project, before, self.it)

    def test_sponsoring_manager_confirms(self):
        project = self.completed()
        self.login(self.mgr)
        self.assertContains(self.client.get(project.get_absolute_url()), "Confirm benefits")
        self.client.post(reverse("projects:confirm_benefits", args=[project.pk]))
        project.refresh_from_db()
        self.assertEqual(project.benefits_verified_by, self.mgr)
        self.assertIsNotNone(project.benefits_verified_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.APPROVAL, object_id=str(project.pk)).exists())

    def test_others_cannot_confirm(self):
        project = self.completed()
        for user in (self.it, self.admin, self.mgr_other):
            self.assertFalse(services.can_confirm_benefits(user, project))
            self.login(user)
            self.assertIn(self.client.post(reverse("projects:confirm_benefits", args=[project.pk])).status_code,
                          (403, 404))
        project.refresh_from_db()
        self.assertIsNone(project.benefits_verified_by)

    def test_unfinished_project_cannot_be_confirmed(self):
        project = self.project(benefits="Expected: fewer complaints")
        self.assertFalse(services.can_confirm_benefits(self.mgr, project))
        with self.assertRaises(PermissionDenied):
            services.confirm_benefits(project, self.mgr)

    def test_it_cannot_record_confirmation_through_the_form(self):
        project = self.completed()
        self.login(self.it)
        self.client.post(reverse("projects:edit", args=[project.pk]), {
            "name": project.name, "status": "completed", "priority": "medium", "owner": self.it.pk,
            "sponsor_department": self.dept.pk, "description": "x", "business_objective": "y",
            "final_outcome": "done", "benefits": project.benefits, "benefits_verified_by": self.mgr.pk})
        project.refresh_from_db()
        self.assertIsNone(project.benefits_verified_by)

    def test_editing_benefits_withdraws_confirmation(self):
        project = services.confirm_benefits(self.completed(), self.mgr)
        before = Project.objects.get(pk=project.pk)
        project.documented_savings, project.savings_evidence, project.currency = Decimal("5000"), "Invoices", "USD"
        services.update_project(project, before, self.it)
        project.refresh_from_db()
        self.assertIsNone(project.benefits_verified_by)
        self.assertIn("confirmation withdrawn", project.updates.first().body)
