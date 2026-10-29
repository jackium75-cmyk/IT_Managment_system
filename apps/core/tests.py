from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.roles import Role

from .models import AuditLog, SystemSetting
from .testing import HotelTestCase


class AuditLogTests(HotelTestCase):
    def test_entries_are_immutable(self):
        entry = AuditLog.objects.create(action=AuditLog.Action.OTHER, message="x")
        entry.message = "changed"
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.delete()
        with self.assertRaises(ValidationError):
            AuditLog.objects.all().update(message="y")
        with self.assertRaises(ValidationError):
            AuditLog.objects.all().delete()

    def test_audit_log_page_admin_only(self):
        admin = self.make_user("adm", Role.ADMIN)
        it = self.make_user("it", Role.IT_OFFICER)
        self.login(it)
        self.assertEqual(self.client.get(reverse("core:audit_log")).status_code, 403)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ACCESS_DENIED, actor=it).exists())
        self.login(admin)
        self.assertEqual(self.client.get(reverse("core:audit_log")).status_code, 200)

    def test_audit_log_filters(self):
        admin = self.make_user("adm", Role.ADMIN)
        AuditLog.objects.create(action=AuditLog.Action.CONFIG_CHANGE, object_repr="needle-record")
        AuditLog.objects.create(action=AuditLog.Action.OTHER, object_repr="other-record")
        self.login(admin)
        resp = self.client.get(reverse("core:audit_log"), {"action": "config_change"})
        self.assertContains(resp, "needle-record")
        self.assertNotContains(resp, "other-record")


class SystemSettingTests(HotelTestCase):
    def test_singleton(self):
        a = SystemSetting.get()
        SystemSetting(hotel_name="Other").save()
        self.assertEqual(SystemSetting.objects.count(), 1)
        self.assertEqual(SystemSetting.get().hotel_name, "Other")
        self.assertEqual(a.pk, 1)

    def test_admin_can_change_settings_and_it_is_audited(self):
        admin = self.make_user("adm", Role.ADMIN)
        self.login(admin)
        data = {
            "hotel_name": "Grand Lagoon", "hotel_timezone": "Africa/Nairobi", "it_department_email": "",
            "it_department_phone": "101", "max_upload_size_mb": 40, "allowed_upload_extensions": "jpg, PNG,pdf",
            "login_max_failures": 5, "login_lockout_minutes": 15,
            "ticket_auto_close_days": 3, "ticket_reopen_days": 14, "currency": "kes", "maintenance_grace_days": 7, "procurement_requires_approval": "on", "procurement_approval_threshold": "0",
        }
        resp = self.client.post(reverse("core:settings"), data)
        self.assertRedirects(resp, reverse("core:settings"))
        cfg = SystemSetting.get()
        self.assertEqual(cfg.hotel_name, "Grand Lagoon")
        self.assertEqual(cfg.allowed_upload_extensions, "jpg,pdf,png")
        entry = AuditLog.objects.get(action=AuditLog.Action.CONFIG_CHANGE)
        self.assertEqual(entry.changes["hotel_name"][1], "Grand Lagoon")

    def test_rejects_dangerous_extensions_and_bad_timezone(self):
        admin = self.make_user("adm", Role.ADMIN)
        self.login(admin)
        data = {"hotel_name": "H", "hotel_timezone": "Mars/Olympus", "max_upload_size_mb": 10,
                "allowed_upload_extensions": "jpg,exe", "login_max_failures": 5, "login_lockout_minutes": 15}
        resp = self.client.post(reverse("core:settings"), data)
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        self.assertIn("allowed_upload_extensions", form.errors)
        self.assertIn("hotel_timezone", form.errors)
        # Entered values are preserved when validation fails.
        self.assertContains(resp, 'value="H"')

    def test_non_admin_cannot_change_settings(self):
        manager = self.make_user("mgr", Role.MANAGER)
        self.login(manager)
        resp = self.client.post(reverse("core:settings"), {"hotel_name": "Hacked"})
        self.assertEqual(resp.status_code, 403)
        self.assertNotEqual(SystemSetting.get().hotel_name, "Hacked")


class HomeRoutingTests(HotelTestCase):
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("core:home"))
        self.assertRedirects(resp, reverse("accounts:login") + "?next=/")

    def test_role_landing_pages(self):
        self.login(self.make_user("emp"))
        self.assertRedirects(self.client.get(reverse("core:home")), reverse("core:employee_home"))
        self.login(self.make_user("it", Role.IT_OFFICER))
        self.assertRedirects(self.client.get(reverse("core:home")), reverse("dashboard:index"))

    def test_sidebar_hides_admin_section_from_employees(self):
        self.login(self.make_user("emp"))
        resp = self.client.get(reverse("core:employee_home"))
        self.assertNotContains(resp, "System Configuration")
        self.assertNotContains(resp, reverse("accounts:user_list"))


class NavigationTests(HotelTestCase):
    def test_only_most_specific_item_is_active(self):
        from .navigation import sidebar_for

        it = self.make_user("it", Role.IT_OFFICER)
        items = sidebar_for(it, "/tasks/planner/week/")
        active = [i["label"] for i in items if i["active"]]
        self.assertEqual(active, ["Daily Planner"])


class FormMixinTests(HotelTestCase):
    def test_validation_runs_after_subclass_setup(self):
        """Regression: styling must not trigger validation inside __init__."""
        from django import forms

        from .forms import BootstrapFormMixin

        class F(BootstrapFormMixin, forms.Form):
            choice = forms.ChoiceField(choices=[("a", "A"), ("b", "B")])

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.fields["choice"].choices = [("a", "A")]  # restricted after super().__init__

        form = F({"choice": "b"})
        self.assertFalse(form.is_valid())
        self.assertIn("is-invalid", form.fields["choice"].widget.attrs["class"])
