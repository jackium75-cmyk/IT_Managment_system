from datetime import datetime, time, timedelta

from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.core.models import AuditLog
from apps.core.testing import HotelTestCase
from apps.tasks import services
from apps.tasks.models import Task, TaskCategory

from .choices import ActivityType
from .models import ActivityLog
from .timecalc import summarize, time_records


def aware(day, hh, mm=0):
    return timezone.make_aware(datetime.combine(day, time(hh, mm)))


class DiaryTests(HotelTestCase):
    def setUp(self):
        super().setUp()
        self.it = self.make_user("it", Role.IT_OFFICER)
        self.it2 = self.make_user("it2", Role.IT_OFFICER)
        self.admin = self.make_user("adm", Role.ADMIN)
        self.yesterday = timezone.localdate() - timedelta(days=1)
        self.support = TaskCategory.objects.get(name="User support")

    def manual(self, user=None, hh=14, minutes=60, **kw):
        user = user or self.it
        self.login(user)
        data = {"occurred_at": f"{self.yesterday.isoformat()}T{hh:02d}:00", "duration_minutes": minutes,
                "activity_type": ActivityType.MEETING, "title": kw.pop("title", "Meeting with GM")}
        data.update(kw)
        return self.client.post(reverse("diary:create"), data)

    def test_manual_entry_create_edit_void_is_audited(self):
        self.assertRedirects(self.manual(), reverse("diary:list"))
        entry = ActivityLog.objects.get(source=ActivityLog.Source.MANUAL)
        resp = self.client.post(reverse("diary:edit", args=[entry.pk]), {
            "occurred_at": f"{self.yesterday.isoformat()}T14:00", "duration_minutes": 90,
            "activity_type": ActivityType.MEETING, "title": "Meeting with GM"})
        self.assertRedirects(resp, reverse("diary:detail", args=[entry.pk]))
        log = AuditLog.objects.filter(object_id=str(entry.pk), action=AuditLog.Action.UPDATE).get()
        self.assertEqual(log.changes["duration_minutes"], [60, 90])
        self.client.post(reverse("diary:void", args=[entry.pk]), {"reason": "Duplicate"})
        entry.refresh_from_db()
        self.assertTrue(entry.is_voided)
        self.assertEqual(ActivityLog.objects.filter(pk=entry.pk).count(), 1)  # never deleted
        # A voided entry can no longer be edited.
        self.assertEqual(self.client.get(reverse("diary:edit", args=[entry.pk])).status_code, 403)

    def test_manual_entry_cannot_overlap_task_time(self):
        task = services.create_task(Task(title="t", category=self.support, planned_date=self.yesterday,
                                         assigned_to=self.it), self.it)
        services.log_time(task, self.it, started_at=aware(self.yesterday, 14, 30), minutes=30)
        resp = self.manual(hh=14, minutes=60)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("overlaps", str(resp.context["form"].errors))

    def test_system_entries_cannot_be_edited_or_voided(self):
        task = services.create_task(Task(title="t", category=self.support, planned_date=self.yesterday,
                                         assigned_to=self.it), self.it)
        services.change_status(task, Task.Status.COMPLETED, self.it, completion_notes="ok")
        auto = ActivityLog.objects.get(source=ActivityLog.Source.AUTO)
        self.login(self.it)
        self.assertEqual(self.client.get(reverse("diary:edit", args=[auto.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("diary:void", args=[auto.pk]), {"reason": "x"}).status_code, 403)

    def test_diary_privacy(self):
        self.manual(user=self.it2, title="Private meeting")
        entry = ActivityLog.objects.get(title="Private meeting")
        self.login(self.it)
        self.assertEqual(self.client.get(reverse("diary:detail", args=[entry.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("diary:list"), {"user": self.it2.pk}).status_code, 403)
        self.assertNotContains(self.client.get(reverse("diary:list"), {"start": self.yesterday.isoformat()}),
                               "Private meeting")
        self.login(self.admin)
        resp = self.client.get(reverse("diary:list"), {"user": self.it2.pk, "start": self.yesterday.isoformat()})
        self.assertContains(resp, "Private meeting")

    def test_filters_and_search(self):
        self.manual(title="Budget meeting", hh=9)
        self.manual(title="Printer training", hh=11, activity_type=ActivityType.TRAINING)
        resp = self.client.get(reverse("diary:list"), {"start": self.yesterday.isoformat(), "type": "training"})
        self.assertContains(resp, "Printer training")
        self.assertNotContains(resp, "Budget meeting")
        resp = self.client.get(reverse("diary:list"), {"start": self.yesterday.isoformat(), "q": "budget"})
        self.assertContains(resp, "Budget meeting")
        self.assertNotContains(resp, "Printer training")


class TimeRuleTests(HotelTestCase):
    """The single time-counting rule: every minute is counted once."""

    def setUp(self):
        super().setUp()
        self.it = self.make_user("it", Role.IT_OFFICER)
        self.day = timezone.localdate() - timedelta(days=1)
        self.support = TaskCategory.objects.get(name="User support")
        self.emergency = TaskCategory.objects.get(name="Emergency")

    def test_totals(self):
        t = services.create_task(Task(title="Plan", category=self.support, planned_date=self.day,
                                      assigned_to=self.it), self.it)
        e1 = services.log_time(t, self.it, started_at=aware(self.day, 9), minutes=50)
        services.change_status(t, Task.Status.COMPLETED, self.it, completion_notes="done")  # auto entry, no time
        urgent = Task(title="Server down", category=self.emergency, assigned_to=self.it, planned_date=self.day)
        services.record_unplanned(urgent, self.it, worked_from=aware(self.day, 10), worked_minutes=30,
                                  completion_notes="Restarted")
        ActivityLog.objects.create(user=self.it, source=ActivityLog.Source.MANUAL, event=ActivityLog.Event.MANUAL,
                                   activity_type=ActivityType.MEETING, occurred_at=aware(self.day, 11),
                                   duration_minutes=45, title="Meeting")
        voided = services.log_time(t, self.it, started_at=aware(self.day, 13), minutes=20)
        services.void_time_entry(voided, self.it, "mistake")

        start, end = aware(self.day, 0), aware(self.day, 23, 59)
        s = summarize(time_records(start, end, user=self.it))
        self.assertEqual(s["total_minutes"], 50 + 30 + 45)
        self.assertEqual(s["unplanned_minutes"], 30)
        self.assertEqual(s["planned_minutes"], 95)
        by = {k: m for _l, k, m in s["by_activity"]}
        self.assertEqual(by, {"support": 50, "emergency": 30, "meeting": 45})
        # Auto diary entries exist for the logged time but carry no counted duration.
        self.assertIsNone(ActivityLog.objects.get(time_entry=e1).duration_minutes)

        # The diary page shows the same total.
        self.login(self.it)
        resp = self.client.get(reverse("diary:list"), {"start": self.day.isoformat(), "end": self.day.isoformat()})
        self.assertEqual(resp.context["total"], "2 h 5 min")

    def test_auto_entries_cannot_carry_time(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            ActivityLog.objects.create(user=self.it, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.MANUAL,
                                       activity_type="other", occurred_at=timezone.now(), duration_minutes=10,
                                       title="should fail")
