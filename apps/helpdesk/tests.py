import io
import shutil
import tempfile
import zipfile
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.roles import Role
from apps.core.models import AuditLog, SystemSetting
from apps.core.testing import HotelTestCase
from apps.diary.timecalc import summarize, time_records
from apps.evidence.validation import validate_upload
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.tasks import services as task_services
from apps.tasks.models import Task, TaskCategory, TaskTimeEntry

from . import services
from .models import EscalationRule, Ticket, TicketAttachment, TicketCategory, TicketComment, TicketTimeEntry

S = Ticket.Status
TMP_MEDIA = tempfile.mkdtemp(prefix="hotel-it-test-media-")


def png(name="error.png"):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def mp4(name="clip.mp4", size=4096):
    data = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(range(256)) * (size // 256)
    return SimpleUploadedFile(name, data, content_type="video/mp4")


def pdf(name="report.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF", content_type="application/pdf")


def docx(name="doc.docx", macro=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        if macro:
            z.writestr("word/vbaProject.bin", "x")
    return SimpleUploadedFile(name, buf.getvalue())


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class HelpdeskTestCase(HotelTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.emp = self.make_user("rose", email="rose@example.invalid")
        self.emp2 = self.make_user("chris", department=self.other_dept)
        self.mgr = self.make_user("fatma", Role.MANAGER)
        self.mgr_other = self.make_user("felix", Role.MANAGER, department=self.other_dept)
        self.it = self.make_user("ian", Role.IT_OFFICER, email="ian@example.invalid")
        self.it2 = self.make_user("ivy", Role.IT_OFFICER)
        self.admin = self.make_user("adm", Role.ADMIN)
        self.cat = TicketCategory.objects.get(name="Printer / scanner", parent=None)
        self.sub = self.cat.children.get(name="Not printing")

    def ticket(self, user=None, **kw):
        kw.setdefault("subject", "Printer not printing")
        kw.setdefault("description", "Shows error E-05")
        kw.setdefault("category", self.cat)
        return services.create_ticket(Ticket(**kw), user or self.emp)

    def post_ticket(self, files=(), **data):
        payload = {"ticket_type": "incident", "subject": "Reception printer jammed", "category": self.cat.pk,
                   "subcategory": self.sub.pk, "description": "Paper stuck, error light", "impact": "several",
                   "urgency": "stopped", "contact_phone": "101"}
        payload.update(data)
        if files:
            payload["files"] = list(files)
        return self.client.post(reverse("helpdesk:create"), payload)


class EvidenceValidationTests(HelpdeskTestCase):
    def test_accepts_genuine_files(self):
        cfg = SystemSetting.get()
        for f, kind in ((png(), "image"), (mp4(), "video"), (pdf(), "document"), (docx(), "document"),
                        (SimpleUploadedFile("ping.log", b"Reply from 8.8.8.8\n"), "document")):
            self.assertEqual(validate_upload(f, cfg)[1], kind, f.name)

    def test_rejects_dangerous_or_disguised_files(self):
        cfg = SystemSetting.get()
        bad = [
            SimpleUploadedFile("tool.exe", b"MZ\x90\x00"),                      # executable
            SimpleUploadedFile("photo.jpg", b"MZ\x90\x00 not really a jpeg"),   # disguised
            SimpleUploadedFile("page.html", b"<script>alert(1)</script>"),      # script type
            SimpleUploadedFile("image.svg", b"<svg onload=alert(1)>"),          # svg can carry script
            SimpleUploadedFile("notes.txt", b"\x00\x01\x02binary"),             # binary posing as text
            SimpleUploadedFile("broken.png", b"\x89PNG\r\n\x1a\n" + b"garbage"),  # fails Pillow verify
            docx("macro.docx", macro=True),                                     # macros
            SimpleUploadedFile("empty.pdf", b""),
        ]
        for f in bad:
            with self.assertRaises(ValidationError, msg=f.name):
                validate_upload(f, cfg)

    def test_size_limit(self):
        cfg = SystemSetting.get()
        cfg.max_upload_size_mb = 1
        with self.assertRaises(ValidationError):
            validate_upload(mp4(size=2 * 1024 * 1024), cfg)

    def test_admin_cannot_enable_unverifiable_types(self):
        self.login(self.admin)
        resp = self.client.post(reverse("core:settings"), {
            "hotel_name": "H", "hotel_timezone": "UTC", "max_upload_size_mb": 10, "allowed_upload_extensions": "jpg,zip",
            "login_max_failures": 5, "login_lockout_minutes": 15, "ticket_auto_close_days": 3, "ticket_reopen_days": 14,
            "currency": "USD", "maintenance_grace_days": 7, "procurement_requires_approval": "on", "procurement_approval_threshold": "0"})
        self.assertIn("allowed_upload_extensions", resp.context["form"].errors)


class TicketCreationTests(HelpdeskTestCase):
    def test_employee_reports_with_evidence(self):
        self.login(self.emp)
        resp = self.post_ticket(files=[png(), mp4()])
        ticket = Ticket.objects.get()
        self.assertRedirects(resp, ticket.get_absolute_url())
        self.assertTrue(ticket.reference.startswith(f"INC-{timezone.localdate().year}-"))
        self.assertEqual(ticket.priority, "high")  # several people × stopped
        self.assertEqual(ticket.requester, self.emp)
        self.assertEqual(ticket.department, self.dept)
        self.assertIsNotNone(ticket.response_due_at)
        files = list(ticket.attachments.all())
        self.assertEqual(len(files), 2)
        for att in files:
            self.assertTrue(att.file.name.startswith("evidence/"))
            self.assertNotIn(att.original_name.rsplit(".", 1)[0], att.file.name)  # random storage name
            self.assertEqual(len(att.sha256), 64)
            self.assertEqual(att.uploaded_by, self.emp)
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.UPLOAD, object_id=str(ticket.pk)).count(), 2)
        # Every IT staff member is notified of the new ticket; the requester is not.
        self.assertEqual(set(Notification.objects.filter(kind=Notification.Kind.TICKET_NEW).values_list("recipient__username", flat=True)),
                         {"ian", "ivy", "adm"})

    def test_bad_file_keeps_form_input_and_creates_nothing(self):
        self.login(self.emp)
        resp = self.post_ticket(files=[SimpleUploadedFile("virus.exe", b"MZ")], subject="Kept subject")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("files", resp.context["form"].errors)
        self.assertContains(resp, 'value="Kept subject"')
        self.assertFalse(Ticket.objects.exists())

    def test_subcategory_must_match_category(self):
        self.login(self.emp)
        other = TicketCategory.objects.get(name="Internet / Wi-Fi", parent=None)
        resp = self.post_ticket(category=other.pk, subcategory=self.sub.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Ticket.objects.exists())

    def test_critical_incident_alerts_it_and_admins(self):
        self.ticket(impact="guests", urgency="stopped")
        self.assertEqual(Notification.objects.filter(kind=Notification.Kind.CRITICAL).count(), 3)

    def test_it_can_log_on_behalf_of_employee(self):
        self.login(self.it)
        self.post_ticket(requester=self.emp.pk)
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.requester, self.emp)
        self.assertIn("on behalf", ticket.status_history.first().note)


class VisibilityTests(HelpdeskTestCase):
    def setUp(self):
        super().setUp()
        self.t = self.ticket()
        self.att = services.save_attachments(self.t, [(png(), "png", "image", "image/png")], self.emp)[0]
        self.internal = services.save_attachments(self.t, [(pdf(), "pdf", "document", "application/pdf")], self.it,
                                                  is_internal=True)[0]
        services.add_comment(self.t, self.it, "Checked fuser temperature", kind=TicketComment.Kind.INTERNAL)

    def test_who_can_open_the_ticket(self):
        url = self.t.get_absolute_url()
        for user, code in ((self.emp, 200), (self.mgr, 200), (self.it, 200), (self.admin, 200),
                           (self.emp2, 403), (self.mgr_other, 403)):
            self.login(user)
            self.assertEqual(self.client.get(url).status_code, code, user.username)
            self.assertEqual(self.client.get(reverse("helpdesk:attachment", args=[self.att.pk])).status_code,
                             code, user.username)

    def test_internal_notes_and_files_hidden_from_requester(self):
        self.login(self.emp)
        resp = self.client.get(self.t.get_absolute_url())
        self.assertNotContains(resp, "Checked fuser temperature")
        self.assertEqual(self.client.get(reverse("helpdesk:attachment", args=[self.internal.pk])).status_code, 403)
        self.login(self.it)
        self.assertContains(self.client.get(self.t.get_absolute_url()), "Checked fuser temperature")

    def test_files_cannot_be_reached_by_guessing_paths(self):
        self.login(self.emp)
        self.assertEqual(self.client.get("/protected-media/" + self.att.file.name).status_code, 404)
        self.assertEqual(self.client.get("/media/" + self.att.file.name).status_code, 404)
        self.client.logout()
        resp = self.client.get(reverse("helpdesk:attachment", args=[self.att.pk]))
        self.assertEqual(resp.status_code, 302)  # sign-in required

    def test_safe_serving_headers_and_range(self):
        video = services.save_attachments(self.t, [(mp4(size=8192), "mp4", "video", "video/mp4")], self.emp)[0]
        self.login(self.emp)
        resp = self.client.get(reverse("helpdesk:attachment", args=[video.pk]), HTTP_RANGE="bytes=10-19")
        self.assertEqual(resp.status_code, 206)
        body = b"".join(resp.streaming_content)
        with open(video.file.path, "rb") as fh:
            self.assertEqual(body, fh.read()[10:20])
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", resp["Content-Security-Policy"])
        resp = self.client.get(reverse("helpdesk:attachment", args=[self.att.pk]) + "?download=1")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("sandbox", resp["Content-Security-Policy"])

    def test_list_shows_only_own_tickets_for_employee(self):
        other = self.ticket(user=self.emp2, subject="Chris printer")
        self.login(self.emp)
        resp = self.client.get(reverse("helpdesk:list"))
        self.assertContains(resp, self.t.reference)
        self.assertNotContains(resp, other.reference)
        self.login(self.mgr_other)
        resp = self.client.get(reverse("helpdesk:list"), {"queue": "department"})
        self.assertContains(resp, other.reference)
        self.assertNotContains(resp, self.t.reference)

    def test_employee_cannot_use_it_actions(self):
        self.login(self.emp)
        for url in (reverse("helpdesk:status", args=[self.t.pk]), reverse("helpdesk:priority", args=[self.t.pk]),
                    reverse("helpdesk:log_time", args=[self.t.pk])):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(reverse("helpdesk:assign", args=[self.t.pk]), {"me": "1"}).status_code, 403)
        # Employees have no "kind" field: a forged internal note is saved as a normal public reply.
        self.assertEqual(self.client.post(reverse("helpdesk:comment", args=[self.t.pk]),
                                          {"body": "x", "kind": "internal"}).status_code, 302)
        self.assertFalse(TicketComment.objects.filter(body="x", kind="internal").exists())
        self.assertTrue(TicketComment.objects.filter(body="x", kind="public").exists())
        with self.assertRaises(PermissionDenied):
            services.add_comment(self.t, self.emp, "y", kind=TicketComment.Kind.INTERNAL)


class LifecycleTests(HelpdeskTestCase):
    def test_full_lifecycle_with_waiting_feedback_and_reopen(self):
        t = self.ticket()
        t = services.change_status(t, S.ACKNOWLEDGED, self.it)
        self.assertIsNotNone(t.first_response_at)
        self.assertEqual(t.assigned_to, self.it)
        due_before = t.resolution_due_at

        start = timezone.now()
        with mock.patch("django.utils.timezone.now", return_value=start):
            services.change_status(t, S.WAITING, self.it, waiting_on="requester", note="Which tray is jammed?")
        self.assertTrue(Notification.objects.filter(recipient=self.emp, kind=Notification.Kind.TICKET_INFO).exists())
        with mock.patch("django.utils.timezone.now", return_value=start + timedelta(minutes=90)):
            services.add_comment(t, self.emp, "Tray 2")
        t.refresh_from_db()
        self.assertEqual(t.status, S.IN_PROGRESS)  # the requester's answer resumes work
        self.assertEqual(t.paused_minutes, 90)
        self.assertEqual(t.resolution_due_at, due_before + timedelta(minutes=90))

        with self.assertRaises(ValidationError):
            services.change_status(t, S.RESOLVED, self.it, resolution_details="")
        t = services.change_status(t, S.RESOLVED, self.it, resolution_details="Cleared jam, replaced roller",
                                   root_cause="Worn roller")
        self.assertTrue(Notification.objects.filter(recipient=self.emp, kind=Notification.Kind.TICKET_RESOLVED).exists())

        with self.assertRaises(PermissionDenied):
            services.confirm_resolution(t, self.emp2)
        t = services.confirm_resolution(t, self.emp, rating=5, comment="Quick fix")
        self.assertEqual((t.status, t.feedback_rating, t.confirmed_by_requester), (S.CLOSED, 5, True))

        t = services.reopen(t, self.emp, "Jammed again")
        self.assertEqual((t.status, t.reopen_count), (S.IN_PROGRESS, 1))
        statuses = list(t.status_history.order_by("id").values_list("to_status", flat=True))
        self.assertEqual(statuses, ["new", "acknowledged", "waiting", "in_progress", "resolved", "closed", "in_progress"])
        self.assertTrue(t.status_history.filter(is_reopen=True).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.it, kind=Notification.Kind.TICKET_REOPENED).exists())

    def test_invalid_transitions_and_reasons(self):
        t = self.ticket()
        with self.assertRaises(ValidationError):
            services.change_status(t, S.WAITING, self.it, waiting_on="", note="x")
        with self.assertRaises(ValidationError):
            services.change_status(t, S.CLOSED, self.it, note="")  # closing unresolved needs a reason
        services.change_status(t, S.CLOSED, self.it, note="Duplicate of another ticket")
        with self.assertRaises(ValidationError):
            services.change_status(t, S.IN_PROGRESS, self.it)
        with self.assertRaises(PermissionDenied):
            services.change_status(t, S.RESOLVED, self.emp, resolution_details="x")

    def test_reopen_window(self):
        t = self.ticket()
        services.change_status(t, S.RESOLVED, self.it, resolution_details="Done")
        services.confirm_resolution(t, self.emp)
        Ticket.objects.filter(pk=t.pk).update(closed_at=timezone.now() - timedelta(days=30))
        t.refresh_from_db()
        self.assertFalse(services.can_reopen(self.emp, t))
        with self.assertRaises(PermissionDenied):
            services.reopen(t, self.emp, "again")
        self.assertTrue(services.can_reopen(self.it, t))  # IT can always reopen

    def test_auto_close(self):
        t = self.ticket()
        services.change_status(t, S.RESOLVED, self.it, resolution_details="Done")
        self.assertEqual(services.auto_close_resolved(), 0)
        later = timezone.now() + timedelta(days=SystemSetting.get().ticket_auto_close_days, hours=1)
        self.assertEqual(services.auto_close_resolved(later), 1)
        t.refresh_from_db()
        self.assertEqual(t.status, S.CLOSED)
        self.assertFalse(t.confirmed_by_requester)

    def test_priority_change_requires_reason_and_resets_targets(self):
        t = self.ticket(impact="one", urgency="minor")
        self.assertEqual(t.priority, "low")
        with self.assertRaises(ValidationError):
            services.change_priority(t, "critical", self.it, "")
        t = services.change_priority(t, "critical", self.it, "Affects night audit")
        self.assertEqual(t.resolution_due_at, t.created_at + timedelta(minutes=240))
        self.assertTrue(t.priority_overridden)

    def test_status_view_flow(self):
        t = self.ticket()
        self.login(self.it)
        resp = self.client.post(reverse("helpdesk:status", args=[t.pk]), {"status": "resolved",
                                                                          "resolution_details": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("resolution_details", resp.context["form"].errors)
        resp = self.client.post(reverse("helpdesk:status", args=[t.pk]), {"status": "resolved",
                                                                          "resolution_details": "Reset spooler"})
        self.assertRedirects(resp, t.get_absolute_url())
        self.login(self.emp)
        resp = self.client.post(reverse("helpdesk:confirm", args=[t.pk]), {"rating": "4"})
        self.assertRedirects(resp, t.get_absolute_url())
        t.refresh_from_db()
        self.assertEqual(t.status, S.CLOSED)

    def test_comment_with_after_fix_photo(self):
        t = self.ticket()
        self.login(self.it)
        self.client.post(reverse("helpdesk:comment", args=[t.pk]), {
            "body": "Printer working again", "kind": "public", "stage": "after_fix", "files": [png("working.png")]})
        att = TicketAttachment.objects.get(ticket=t)
        self.assertEqual((att.stage, att.is_internal, att.comment.body), ("after_fix", False, "Printer working again"))
        t.refresh_from_db()
        self.assertIsNotNone(t.first_response_at)


class EscalationAndNotificationTests(HelpdeskTestCase):
    def test_critical_unassigned_escalates_once(self):
        t = self.ticket(impact="guests", urgency="stopped")
        rule = EscalationRule.objects.get(trigger="unassigned")
        later = timezone.now() + timedelta(minutes=20)
        before = Notification.objects.filter(kind=Notification.Kind.ESCALATION).count()
        self.assertGreaterEqual(services.run_escalations(later), 1)
        self.assertTrue(t.escalations.filter(rule=rule).exists())
        self.assertGreater(Notification.objects.filter(kind=Notification.Kind.ESCALATION).count(), before)
        self.assertEqual(services.run_escalations(later), 0)  # never twice

    def test_waiting_ticket_does_not_breach(self):
        t = self.ticket()
        services.change_status(t, S.WAITING, self.it, waiting_on="vendor", note="RMA with vendor")
        far = timezone.now() + timedelta(days=10)
        t.refresh_from_db()
        self.assertFalse(Ticket.objects.overdue(far).filter(pk=t.pk).exists())

    def test_duplicates_suppressed(self):
        notify([self.it], Notification.Kind.OVERDUE, "x", dedupe_key="k")
        notify([self.it], Notification.Kind.OVERDUE, "x", dedupe_key="k")
        self.assertEqual(Notification.objects.filter(recipient=self.it, dedupe_key="k").count(), 1)

    def test_email_sent_after_commit_when_enabled(self):
        cfg = SystemSetting.get()
        cfg.email_notifications_enabled = True
        cfg.save()
        with self.captureOnCommitCallbacks(execute=True):
            t = self.ticket()
        self.assertTrue(any("ian@example.invalid" in m.to for m in mail.outbox))
        self.emp.profile.email_notifications = False
        self.emp.profile.save()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.change_status(t, S.ACKNOWLEDGED, self.it)
        self.assertFalse(any("rose@example.invalid" in m.to for m in mail.outbox))  # opted out

    def test_notification_centre(self):
        n = notify([self.it], Notification.Kind.TICKET_NEW, "Hello", url="/helpdesk/")[0]
        self.login(self.emp)
        self.assertEqual(self.client.get(reverse("notifications:open", args=[n.pk])).status_code, 404)
        self.login(self.it)
        self.assertContains(self.client.get(reverse("notifications:list")), "Hello")
        self.assertRedirects(self.client.get(reverse("notifications:open", args=[n.pk])), "/helpdesk/",
                             fetch_redirect_response=False)
        n.refresh_from_db()
        self.assertTrue(n.is_read)

    def test_overdue_task_reminder_daily(self):
        cat = TaskCategory.objects.get(name="User support")
        task_services.create_task(Task(title="Old", category=cat, planned_date=timezone.localdate() - timedelta(days=2),
                                       assigned_to=self.it), self.it)
        services.remind_overdue_tasks()
        services.remind_overdue_tasks()
        self.assertEqual(Notification.objects.filter(recipient=self.it, kind=Notification.Kind.OVERDUE).count(), 1)


class TimeRuleTests(HelpdeskTestCase):
    """Time on a task linked to a ticket counts against the ticket — once."""

    def setUp(self):
        super().setUp()
        self.cat_task = TaskCategory.objects.get(name="User support")
        self.t = self.ticket()
        self.yesterday = timezone.localdate() - timedelta(days=1)

    def at(self, hh):
        from datetime import datetime, time

        return timezone.make_aware(datetime.combine(self.yesterday, time(hh)))

    def task(self, **kw):
        return task_services.create_task(Task(title="Replace roller", category=self.cat_task, planned_date=self.yesterday,
                                              assigned_to=self.it, **kw), self.it)

    def totals(self):
        return summarize(time_records(self.at(0), self.at(23), user=self.it))

    def test_linked_task_time_is_stored_on_ticket(self):
        task = self.task(related_ticket=self.t)
        task_services.log_time(task, self.it, started_at=self.at(9), minutes=40)
        self.assertFalse(TaskTimeEntry.objects.filter(task=task).exists())
        entry = TicketTimeEntry.objects.get(ticket=self.t)
        self.assertEqual((entry.via_task, entry.minutes), (task, 40))
        services.log_time(self.t, self.it, started_at=self.at(10), minutes=20)
        s = self.totals()
        self.assertEqual(s["total_minutes"], 60)
        self.assertEqual(self.t.recorded_minutes, 60)
        self.assertEqual(task.recorded_minutes, 40)  # shown on the task, counted once on the ticket
        self.assertEqual(s["planned_minutes"], 40)   # done through a planned task
        self.assertEqual(s["unplanned_minutes"], 20)  # reactive ticket work

    def test_linking_moves_existing_task_time(self):
        task = self.task()
        task_services.log_time(task, self.it, started_at=self.at(9), minutes=30)
        self.assertEqual(self.totals()["total_minutes"], 30)
        task_services.link_to_ticket(task, self.t, self.it)
        self.assertEqual(self.totals()["total_minutes"], 30)  # unchanged: moved, not duplicated
        self.assertTrue(TaskTimeEntry.objects.get(task=task).is_voided)
        self.assertEqual(TicketTimeEntry.objects.get(ticket=self.t, via_task=task).minutes, 30)
        with self.assertRaises(ValidationError):
            task_services.link_to_ticket(task, self.t, self.it)

    def test_one_running_timer_across_tasks_and_tickets(self):
        task = self.task()
        task_services.start_timer(task, self.it)
        _e, stopped = services.start_timer(self.t, self.it)
        self.assertIsInstance(stopped, TaskTimeEntry)
        self.t.refresh_from_db()
        self.assertEqual((self.t.status, self.t.assigned_to), (S.IN_PROGRESS, self.it))
        running = task_services.running_timer(self.it)
        self.assertIsInstance(running, TicketTimeEntry)
        services.change_status(self.t, S.RESOLVED, self.it, resolution_details="ok")
        self.assertIsNone(task_services.running_timer(self.it))  # resolving stops the ticket timer

    def test_overlap_rejected_across_sources(self):
        services.log_time(self.t, self.it, started_at=self.at(9), minutes=60)
        with self.assertRaises(ValidationError):
            task_services.log_time(self.task(), self.it, started_at=self.at(9), minutes=30)

    def test_create_task_from_ticket_view(self):
        self.login(self.it)
        resp = self.client.get(reverse("helpdesk:create_task", args=[self.t.pk]))
        self.assertRedirects(resp, reverse("tasks:create") + f"?ticket={self.t.pk}", fetch_redirect_response=False)
        resp = self.client.get(resp.url)
        self.assertContains(resp, self.t.reference)
        form = resp.context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None}
        data.update({"priority": self.t.priority, "planned_date": timezone.localdate().isoformat(),
                     "assigned_to": self.it.pk})
        resp = self.client.post(reverse("tasks:create"), data)
        task = Task.objects.get(related_ticket=self.t)
        self.assertRedirects(resp, task.get_absolute_url())


class DashboardTests(HelpdeskTestCase):
    def test_helpdesk_cards(self):
        a = self.ticket(impact="guests", urgency="stopped")
        b = self.ticket(user=self.emp2)
        services.change_status(b, S.RESOLVED, self.it, resolution_details="ok")
        self.login(self.it)
        resp = self.client.get(reverse("dashboard:index"))
        cards = {c["label"]: c["value"] for c in resp.context["card_sections"]["Helpdesk now"]}
        self.assertEqual(cards["New requests"], 1)
        self.assertEqual(cards["Critical / high open"], 1)
        self.assertEqual(cards["Resolved today"], 1)
        self.assertEqual(cards["Awaiting requester feedback"], 1)
        period = {c["label"]: c["value"] for c in resp.context["card_sections"]["Helpdesk in selected period"]}
        self.assertEqual(period["Tickets received"], 2)
        # Managers only see their department's tickets.
        self.login(self.mgr)
        resp = self.client.get(reverse("dashboard:index"))
        cards = {c["label"]: c["value"] for c in resp.context["card_sections"]["Helpdesk now"]}
        self.assertEqual(cards["New requests"], 1)
        self.assertEqual(cards["Resolved today"], 0)
        self.assertNotIn("Today's work", resp.context["card_sections"])
        self.assertTrue(a)

    def test_pages_render(self):
        t = self.ticket()
        self.login(self.it)
        for url in (reverse("helpdesk:list"), reverse("helpdesk:list") + "?queue=overdue&sort=due",
                    reverse("helpdesk:create"), t.get_absolute_url(), reverse("helpdesk:status", args=[t.pk]),
                    reverse("helpdesk:priority", args=[t.pk]), reverse("helpdesk:log_time", args=[t.pk]),
                    reverse("notifications:list"), reverse("dashboard:index")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.login(self.admin)
        for url in (reverse("helpdesk:category_list"), reverse("helpdesk:sla_list"), reverse("helpdesk:rule_create")):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.login(self.emp)
        for url in (reverse("core:employee_home"), reverse("helpdesk:list"), reverse("helpdesk:create"),
                    t.get_absolute_url()):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get(reverse("helpdesk:sla_list")).status_code, 403)
