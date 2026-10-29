from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.core.models import AuditLog
from apps.core.testing import HotelTestCase


def card(resp, section, label):
    for c in resp.context["card_sections"][section]:
        if c["label"] == label:
            return c
    raise AssertionError(f"card {label!r} not found")


class DashboardTests(HotelTestCase):
    def test_access_by_role(self):
        self.login(self.make_user("emp"))
        self.assertEqual(self.client.get(reverse("dashboard:index")).status_code, 403)
        for role in (Role.ADMIN, Role.IT_OFFICER, Role.MANAGER):
            self.login(self.make_user(f"u_{role}", role))
            self.assertEqual(self.client.get(reverse("dashboard:index")).status_code, 200)

    def test_counts_come_from_database(self):
        admin = self.make_user("adm", Role.ADMIN)
        self.make_user("e1")
        self.make_user("e2", department=self.other_dept)
        self.make_user("old", is_active=False)
        self.make_user("it", Role.IT_OFFICER)
        self.login(admin)
        resp = self.client.get(reverse("dashboard:index"))
        self.assertEqual(card(resp, "Organisation", "Active user accounts")["value"], 4)
        self.assertEqual(card(resp, "Organisation", "Employees")["value"], 2)
        self.assertEqual(card(resp, "Organisation", "IT support officers")["value"], 1)
        resp = self.client.get(reverse("dashboard:index"), {"department": self.other_dept.pk})
        self.assertEqual(card(resp, "Organisation", "Active user accounts")["value"], 1)
        # Clicking the card opens the matching filtered list.
        url = card(resp, "Organisation", "Employees")["url"]
        self.assertIn(f"department={self.other_dept.pk}", url)
        self.assertIn("role=employee", url)

    def test_period_filter_on_activity(self):
        admin = self.make_user("adm", Role.ADMIN)
        AuditLog.objects.create(action=AuditLog.Action.LOGIN_FAILED)
        AuditLog.objects.create(action=AuditLog.Action.LOGIN_FAILED)
        self.login(admin)
        today = timezone.localdate()
        resp = self.client.get(reverse("dashboard:index"), {"start": today.isoformat(), "end": today.isoformat()})
        self.assertEqual(card(resp, "System activity in period", "Failed sign-ins")["value"], 2)
        yesterday = today - timedelta(days=1)
        resp = self.client.get(reverse("dashboard:index"), {"start": yesterday.isoformat(), "end": yesterday.isoformat()})
        self.assertEqual(card(resp, "System activity in period", "Failed sign-ins")["value"], 0)

    def test_manager_locked_to_own_department(self):
        mgr = self.make_user("mgr", Role.MANAGER)
        self.make_user("other", department=self.other_dept)
        self.login(mgr)
        resp = self.client.get(reverse("dashboard:index"), {"department": self.other_dept.pk})
        self.assertEqual(resp.context["filters"].department_id, self.dept.pk)
        self.assertEqual(card(resp, "Organisation", "Active user accounts")["value"], 1)
        self.assertNotIn("System activity in period", resp.context["card_sections"])
        self.assertIsNone(card(resp, "Organisation", "Employees")["url"])
