from datetime import date, datetime, time, timedelta
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.core.models import AuditLog
from apps.core.testing import HotelTestCase
from apps.diary.models import ActivityLog

from . import recurrence, services
from .models import RecurringTask, Task, TaskCategory, TaskComment, TaskScheduleChange, TaskTimeEntry


def aware(day, hh, mm=0):
    return timezone.make_aware(datetime.combine(day, time(hh, mm)))


class TaskTestCase(HotelTestCase):
    def setUp(self):
        super().setUp()
        self.it = self.make_user("it", Role.IT_OFFICER)
        self.it2 = self.make_user("it2", Role.IT_OFFICER)
        self.admin = self.make_user("adm", Role.ADMIN)
        self.cat = TaskCategory.objects.get(name="User support")
        self.today = timezone.localdate()

    def task(self, user=None, day=None, **kw):
        user = user or self.it
        kw.setdefault("title", "Fix printer")
        kw.setdefault("category", self.cat)
        return services.create_task(Task(planned_date=day or self.today, assigned_to=user, **kw), user)


class TaskCreationTests(TaskTestCase):
    def test_numbers_are_sequential_and_unique(self):
        a, b = self.task(), self.task()
        year = self.today.year
        self.assertEqual(a.number, f"TSK-{year}-00001")
        self.assertEqual(b.number, f"TSK-{year}-00002")
        self.assertEqual(a.original_planned_date, self.today)

    def test_create_via_form(self):
        self.login(self.it)
        resp = self.client.post(reverse("tasks:create"), {
            "title": "Check Wi-Fi", "category": self.cat.pk, "priority": "high",
            "planned_date": self.today.isoformat(), "scheduled_start": "09:00", "scheduled_end": "10:00",
            "assigned_to": self.it.pk, "estimated_minutes": 30,
        })
        task = Task.objects.get(title="Check Wi-Fi")
        self.assertRedirects(resp, task.get_absolute_url())
        self.assertEqual(task.created_by, self.it)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CREATE, object_id=str(task.pk)).exists())

    def test_form_validation_keeps_input(self):
        self.login(self.it)
        resp = self.client.post(reverse("tasks:create"), {
            "title": "Kept title", "category": self.cat.pk, "priority": "medium",
            "planned_date": self.today.isoformat(), "scheduled_start": "10:00", "scheduled_end": "09:00",
            "assigned_to": self.it.pk,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("scheduled_end", resp.context["form"].errors)
        self.assertContains(resp, 'value="Kept title"')

    def test_cannot_assign_to_non_it_staff(self):
        emp = self.make_user("emp")
        with self.assertRaises(ValidationError):
            services.create_task(Task(title="x", category=self.cat, planned_date=self.today, assigned_to=emp), self.it)


class PermissionTests(TaskTestCase):
    def test_non_it_roles_blocked(self):
        t = self.task()
        for role in (Role.EMPLOYEE, Role.MANAGER):
            self.login(self.make_user(f"u_{role}", role))
            for url in (reverse("tasks:list"), reverse("tasks:planner"), reverse("tasks:detail", args=[t.pk]),
                        reverse("diary:list"), reverse("tasks:recurring_list")):
                self.assertEqual(self.client.get(url).status_code, 403, (role, url))

    def test_officer_cannot_change_colleagues_task(self):
        t = self.task(user=self.it2)
        self.login(self.it)
        self.assertEqual(self.client.get(reverse("tasks:detail", args=[t.pk])).status_code, 200)  # may view
        resp = self.client.post(reverse("tasks:complete", args=[t.pk]), {"completion_notes": "done"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(reverse("tasks:timer_start", args=[t.pk]))
        self.assertEqual(resp.status_code, 403)
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.PLANNED)
        # Administrators may change any task.
        services.change_status(t, Task.Status.COMPLETED, self.admin, completion_notes="by admin")

    def test_only_admin_manages_categories(self):
        self.login(self.it)
        self.assertEqual(self.client.get(reverse("tasks:category_list")).status_code, 403)
        self.login(self.admin)
        self.client.post(reverse("tasks:category_create"), {"name": "Guest Wi-Fi", "activity_type": "support",
                                                            "is_active": "on"})
        self.assertTrue(TaskCategory.objects.filter(name="Guest Wi-Fi").exists())


class StatusRuleTests(TaskTestCase):
    def test_reason_required_for_waiting_blocked_cancelled(self):
        t = self.task()
        for status in (Task.Status.WAITING_INFO, Task.Status.WAITING_PARTS, Task.Status.BLOCKED, Task.Status.CANCELLED):
            with self.assertRaises(ValidationError):
                services.change_status(t, status, self.it, reason="  ")
        services.change_status(t, Task.Status.BLOCKED, self.it, reason="Switch room locked")
        t.refresh_from_db()
        self.assertEqual(t.delay_reason, "Switch room locked")
        c = t.comments.get(kind=TaskComment.Kind.STATUS)
        self.assertEqual((c.old_status, c.new_status), (Task.Status.PLANNED, Task.Status.BLOCKED))

    def test_completion_requires_notes_and_records_everything(self):
        t = self.task()
        with self.assertRaises(ValidationError):
            services.change_status(t, Task.Status.COMPLETED, self.it, completion_notes="")
        services.change_status(t, Task.Status.COMPLETED, self.it, completion_notes="Replaced fuser")
        t.refresh_from_db()
        self.assertIsNotNone(t.completed_at)
        self.assertEqual(t.completion_notes, "Replaced fuser")
        self.assertTrue(ActivityLog.objects.filter(task=t, event=ActivityLog.Event.TASK_COMPLETED,
                                                   source=ActivityLog.Source.AUTO).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.STATUS_CHANGE, object_id=str(t.pk)).exists())

    def test_reopen_requires_reason_and_clears_completion(self):
        t = self.task()
        services.change_status(t, Task.Status.COMPLETED, self.it, completion_notes="ok")
        with self.assertRaises(ValidationError):
            services.change_status(t, Task.Status.IN_PROGRESS, self.it)
        services.change_status(t, Task.Status.IN_PROGRESS, self.it, reason="Printer jammed again")
        t.refresh_from_db()
        self.assertIsNone(t.completed_at)
        self.assertTrue(ActivityLog.objects.filter(task=t, event=ActivityLog.Event.TASK_REOPENED).exists())

    def test_completed_late_and_overdue(self):
        t = self.task(day=self.today - timedelta(days=2))
        self.assertTrue(t.is_overdue)
        self.assertIn(t, Task.objects.overdue())
        services.change_status(t, Task.Status.COMPLETED, self.it, completion_notes="late")
        t.refresh_from_db()
        self.assertTrue(t.completed_late)
        self.assertFalse(t.is_overdue)


class RescheduleTests(TaskTestCase):
    def test_reschedule_keeps_original_and_history(self):
        t = self.task()
        tomorrow = self.today + timedelta(days=1)
        with self.assertRaises(ValidationError):
            services.reschedule(t, self.it, new_date=tomorrow, reason="")
        services.reschedule(t, self.it, new_date=tomorrow, reason="Vendor visit moved")
        t.refresh_from_db()
        self.assertEqual(t.planned_date, tomorrow)
        self.assertEqual(t.original_planned_date, self.today)
        self.assertTrue(t.was_rescheduled)
        change = TaskScheduleChange.objects.get(task=t)
        self.assertEqual((change.old_date, change.new_date, change.reason), (self.today, tomorrow, "Vendor visit moved"))

    def test_closed_task_cannot_be_rescheduled(self):
        t = self.task()
        services.change_status(t, Task.Status.CANCELLED, self.it, reason="Not needed")
        with self.assertRaises(ValidationError):
            services.reschedule(t, self.it, new_date=self.today + timedelta(days=1), reason="x")

    def test_planner_shows_moved_tasks_and_carry_over(self):
        a, b, done = self.task(title="A"), self.task(title="B"), self.task(title="Done one")
        services.change_status(done, Task.Status.COMPLETED, self.it, completion_notes="ok")
        self.login(self.it)
        tomorrow = self.today + timedelta(days=1)
        resp = self.client.post(reverse("tasks:carry_over"), {
            "day": self.today.isoformat(), "task": [a.pk, b.pk, done.pk], "new_date": tomorrow.isoformat(),
            "reason": "Emergency all afternoon"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Task.objects.filter(planned_date=tomorrow).count(), 2)
        done.refresh_from_db()
        self.assertEqual(done.planned_date, self.today)
        resp = self.client.get(reverse("tasks:planner"), {"date": self.today.isoformat()})
        self.assertContains(resp, "Planned for this day but moved")
        self.assertContains(resp, "Emergency all afternoon")


class TimeTrackingTests(TaskTestCase):
    def test_timer_start_stop(self):
        t = self.task()
        start = timezone.now() - timedelta(minutes=25)
        with mock.patch("django.utils.timezone.now", return_value=start):
            services.start_timer(t, self.it)
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.IN_PROGRESS)
        entry = services.stop_timer(self.it)
        self.assertEqual(entry.minutes, 25)
        self.assertEqual(t.recorded_minutes, 25)
        self.assertTrue(ActivityLog.objects.filter(time_entry=entry, duration_minutes__isnull=True).exists())

    def test_starting_another_timer_stops_the_first(self):
        a, b = self.task(title="A"), self.task(title="B")
        services.start_timer(a, self.it)
        _entry, stopped = services.start_timer(b, self.it)
        self.assertEqual(stopped.task, a)
        self.assertEqual(TaskTimeEntry.objects.filter(user=self.it, ended_at__isnull=True).count(), 1)

    def test_database_allows_only_one_running_timer(self):
        a = self.task()
        TaskTimeEntry.objects.create(task=a, user=self.it, started_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskTimeEntry.objects.create(task=a, user=self.it, started_at=timezone.now())

    def test_completing_stops_running_timer(self):
        t = self.task()
        services.start_timer(t, self.it)
        services.change_status(t, Task.Status.COMPLETED, self.it, completion_notes="ok")
        self.assertFalse(TaskTimeEntry.objects.filter(task=t, ended_at__isnull=True).exists())

    def test_manual_time_rejects_overlap_and_future(self):
        t = self.task()
        yesterday = self.today - timedelta(days=1)
        services.log_time(t, self.it, started_at=aware(yesterday, 9), minutes=60)
        with self.assertRaises(ValidationError) as ctx:
            services.log_time(t, self.it, started_at=aware(yesterday, 9, 30), minutes=30)
        self.assertIn("overlaps", str(ctx.exception))
        services.log_time(t, self.it, started_at=aware(yesterday, 10), minutes=30)  # adjacent is fine
        with self.assertRaises(ValidationError):
            services.log_time(t, self.it, started_at=timezone.now() + timedelta(hours=2), minutes=10)
        # Another officer's time does not clash.
        t2 = self.task(user=self.it2)
        services.log_time(t2, self.it2, started_at=aware(yesterday, 9), minutes=60)

    def test_void_excludes_time_but_keeps_record(self):
        t = self.task()
        entry = services.log_time(t, self.it, started_at=aware(self.today - timedelta(days=1), 9), minutes=45)
        with self.assertRaises(ValidationError):
            services.void_time_entry(entry, self.it, "")
        with self.assertRaises(PermissionDenied):
            services.void_time_entry(entry, self.it2, "not mine")
        services.void_time_entry(entry, self.it, "Logged on wrong task")
        self.assertEqual(t.recorded_minutes, 0)
        self.assertTrue(TaskTimeEntry.objects.filter(pk=entry.pk, is_voided=True).exists())
        # Voided time no longer blocks the slot.
        services.log_time(t, self.it, started_at=aware(self.today - timedelta(days=1), 9), minutes=45)


class InterruptionTests(TaskTestCase):
    def test_unplanned_work_leaves_original_task_untouched(self):
        planned = self.task(title="Install software", scheduled_start=time(10), scheduled_end=time(11))
        services.start_timer(planned, self.it)
        planned.refresh_from_db()
        snapshot = (planned.status, planned.planned_date, planned.scheduled_start, planned.title)
        urgent = Task(title="POS down", category=TaskCategory.objects.get(name="Emergency"), assigned_to=self.it,
                      priority="critical")
        services.record_unplanned(urgent, self.it, interrupted_task=planned, start_timer_now=True)
        planned.refresh_from_db()
        self.assertEqual((planned.status, planned.planned_date, planned.scheduled_start, planned.title), snapshot)
        self.assertTrue(urgent.is_unplanned)
        self.assertEqual(urgent.interrupted_task, planned)
        self.assertEqual(urgent.status, Task.Status.IN_PROGRESS)
        # The original timer was stopped (no double-counted time) and noted on its timeline.
        running = services.running_timer(self.it)
        self.assertEqual(running.task, urgent)
        self.assertTrue(planned.comments.filter(kind=TaskComment.Kind.INTERRUPTION).exists())

    def test_unplanned_already_finished_via_form(self):
        self.login(self.it)
        yesterday = self.today - timedelta(days=1)
        resp = self.client.post(reverse("tasks:unplanned_create"), {
            "title": "Guest cannot print boarding pass", "category": self.cat.pk, "priority": "high",
            "assigned_to": self.it.pk, "mode": "done", "worked_from": f"{yesterday.isoformat()}T16:00",
            "worked_minutes": 20, "completion_notes": "Reinstalled driver",
        })
        task = Task.objects.get(title="Guest cannot print boarding pass")
        self.assertRedirects(resp, task.get_absolute_url())
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(task.recorded_minutes, 20)

    def test_done_mode_requires_details(self):
        self.login(self.it)
        resp = self.client.post(reverse("tasks:unplanned_create"), {
            "title": "X", "category": self.cat.pk, "priority": "high", "assigned_to": self.it.pk, "mode": "done"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("completion_notes", resp.context["form"].errors)
        self.assertFalse(Task.objects.filter(title="X").exists())


class RecurrenceTests(TaskTestCase):
    def schedule(self, **kw):
        kw.setdefault("title", "Check internet")
        kw.setdefault("category", self.cat)
        kw.setdefault("assigned_to", self.it)
        kw.setdefault("created_by", self.it)
        kw.setdefault("start_date", self.today)
        rt = RecurringTask(**kw)
        rt.full_clean()
        rt.save()
        return rt

    def test_occurrence_rules(self):
        daily = RecurringTask(frequency="daily", interval=1, start_date=date(2026, 1, 1), skip_weekends=True)
        self.assertTrue(daily.occurs_on(date(2026, 1, 2)))   # Friday
        self.assertFalse(daily.occurs_on(date(2026, 1, 3)))  # Saturday
        custom = RecurringTask(frequency="custom", interval=3, start_date=date(2026, 1, 1))
        self.assertEqual([d for d in range(1, 11) if custom.occurs_on(date(2026, 1, d))], [1, 4, 7, 10])
        weekly = RecurringTask(frequency="weekly", interval=2, weekdays="0,4", start_date=date(2026, 1, 5))
        mondays_fridays = [d for d in range(5, 31) if weekly.occurs_on(date(2026, 1, d))]
        self.assertEqual(mondays_fridays, [5, 9, 19, 23])
        monthly = RecurringTask(frequency="monthly", interval=1, day_of_month=31, start_date=date(2026, 1, 1))
        self.assertTrue(monthly.occurs_on(date(2026, 2, 28)))   # clamps to last day
        self.assertTrue(monthly.occurs_on(date(2026, 4, 30)))
        self.assertFalse(monthly.occurs_on(date(2026, 3, 30)))
        ended = RecurringTask(frequency="daily", interval=1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
        self.assertFalse(ended.occurs_on(date(2026, 1, 3)))

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.schedule(frequency="weekly", weekdays="")
        with self.assertRaises(ValidationError):
            self.schedule(frequency="monthly")

    def test_generation_is_idempotent(self):
        rt = self.schedule(frequency="daily")
        created = recurrence.generate_for(rt)
        self.assertEqual(created, recurrence.HORIZON_DAYS + 1)
        self.assertEqual(recurrence.generate_for(rt), 0)
        rt.generated_until = None
        self.assertEqual(recurrence.generate_for(rt), 0)  # unique per date, even if the marker is lost
        self.assertEqual(Task.objects.filter(recurrence=rt).count(), recurrence.HORIZON_DAYS + 1)

    def test_edit_replaces_only_untouched_future_tasks(self):
        rt = self.schedule(frequency="daily")
        recurrence.generate_for(rt)
        tomorrow = Task.objects.get(recurrence=rt, planned_date=self.today + timedelta(days=1))
        services.add_comment(tomorrow, self.it, "Bring ladder")
        today_task = Task.objects.get(recurrence=rt, planned_date=self.today)
        rt.title = "Check internet and Wi-Fi"
        rt.save()
        removed, created = recurrence.refresh_template(rt, self.it)
        self.assertEqual(removed, recurrence.HORIZON_DAYS - 1)
        self.assertEqual(created, removed)
        tomorrow.refresh_from_db()
        self.assertEqual(tomorrow.title, "Check internet")  # touched: kept as it was
        self.assertTrue(Task.objects.filter(pk=today_task.pk).exists())  # today never removed
        self.assertEqual(Task.objects.filter(recurrence=rt, title="Check internet and Wi-Fi").count(), created)

    def test_deactivate_via_view_removes_projections(self):
        self.login(self.it)
        resp = self.client.post(reverse("tasks:recurring_create"), {
            "title": "Review backups", "category": self.cat.pk, "priority": "medium", "assigned_to": self.it.pk,
            "frequency": "weekly", "interval": 1, "weekdays": ["0", "1", "2", "3", "4", "5", "6"],
            "start_date": self.today.isoformat(), "is_active": "on"})
        self.assertRedirects(resp, reverse("tasks:recurring_list"))
        rt = RecurringTask.objects.get(title="Review backups")
        self.assertEqual(rt.occurrences.count(), recurrence.HORIZON_DAYS + 1)
        data = {"title": "Review backups", "category": self.cat.pk, "priority": "medium", "assigned_to": self.it.pk,
                "frequency": "weekly", "interval": 1, "weekdays": ["0"], "start_date": self.today.isoformat()}
        self.client.post(reverse("tasks:recurring_edit", args=[rt.pk]), data)  # is_active unticked
        self.assertEqual(rt.occurrences.count(), 1)  # only today's remains

    def test_command(self):
        from io import StringIO

        from django.core.management import call_command

        self.schedule(frequency="daily")
        out = StringIO()
        call_command("generate_recurring_tasks", "--days", "3", stdout=out)
        self.assertIn("4 task(s) generated", out.getvalue())


class ViewSmokeTests(TaskTestCase):
    def test_pages_render(self):
        t = self.task(scheduled_start=time(9), scheduled_end=time(10))
        services.start_timer(t, self.it)
        self.login(self.it)
        for url in [reverse("tasks:list"), reverse("tasks:list") + "?view=all&sort=priority",
                    reverse("tasks:list") + "?view=overdue", reverse("tasks:planner"), reverse("tasks:planner_week"),
                    reverse("tasks:planner_week") + "?user=all", reverse("tasks:detail", args=[t.pk]),
                    reverse("tasks:edit", args=[t.pk]), reverse("tasks:status", args=[t.pk]),
                    reverse("tasks:complete", args=[t.pk]), reverse("tasks:reschedule", args=[t.pk]),
                    reverse("tasks:log_time", args=[t.pk]), reverse("tasks:unplanned_create"),
                    reverse("tasks:recurring_list"), reverse("tasks:recurring_create"), reverse("dashboard:index")]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
        # Running timer is shown in the top bar on every page.
        self.assertContains(self.client.get(reverse("tasks:list")), "Stop")

    def test_list_filters(self):
        self.task(title="Alpha", priority="critical")
        self.task(title="Beta", priority="low")
        self.login(self.it)
        resp = self.client.get(reverse("tasks:list"), {"view": "all", "priority": "critical"})
        self.assertContains(resp, "Alpha")
        self.assertNotContains(resp, "Beta")
        resp = self.client.get(reverse("tasks:list"), {"view": "all", "q": "bet"})
        self.assertContains(resp, "Beta")
        self.assertNotContains(resp, "Alpha")

    def test_open_redirect_is_blocked(self):
        t = self.task()
        self.login(self.it)
        resp = self.client.post(reverse("tasks:timer_start", args=[t.pk]), {"next": "https://evil.example/"})
        self.assertRedirects(resp, t.get_absolute_url())
