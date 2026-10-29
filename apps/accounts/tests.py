from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.models import AuditLog, SystemSetting
from apps.core.testing import PASSWORD, HotelTestCase

from .models import Department, Location, LoginAttempt
from .roles import Role

User = get_user_model()
LOGIN = reverse("accounts:login")


class AuthenticationTests(HotelTestCase):
    def test_login_and_logout_are_audited(self):
        user = self.make_user("rose")
        resp = self.client.post(LOGIN, {"username": "rose", "password": PASSWORD})
        self.assertRedirects(resp, reverse("core:home"), fetch_redirect_response=False)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGIN, actor=user).exists())
        resp = self.client.post(reverse("accounts:logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGOUT, actor=user).exists())

    def test_logout_requires_post(self):
        self.login(self.make_user("rose"))
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)

    def test_inactive_user_cannot_sign_in(self):
        self.make_user("gone", is_active=False)
        resp = self.client.post(LOGIN, {"username": "gone", "password": PASSWORD})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_lockout_after_repeated_failures(self):
        self.make_user("rose")
        limit = SystemSetting.get().login_max_failures
        for _ in range(limit):
            self.client.post(LOGIN, {"username": "rose", "password": "wrong"})
        # Even the correct password is refused while locked.
        resp = self.client.post(LOGIN, {"username": "ROSE", "password": PASSWORD})
        self.assertContains(resp, "Too many failed sign-in attempts")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(LoginAttempt.objects.filter(username="rose", successful=False).count(), limit)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGIN_LOCKED).exists())

    def test_success_resets_failure_count(self):
        self.make_user("rose")
        limit = SystemSetting.get().login_max_failures
        for _ in range(limit - 1):
            self.client.post(LOGIN, {"username": "rose", "password": "wrong"})
        self.client.post(LOGIN, {"username": "rose", "password": PASSWORD})
        self.client.post(reverse("accounts:logout"))
        self.client.post(LOGIN, {"username": "rose", "password": "wrong"})
        self.client.post(LOGIN, {"username": "rose", "password": PASSWORD})
        self.assertIn("_auth_user_id", self.client.session)

    def test_forced_password_change(self):
        user = self.make_user("newbie")
        user.profile.must_change_password = True
        user.profile.save()
        self.login(user)
        self.assertRedirects(self.client.get(reverse("core:employee_home")), reverse("accounts:password_change"))
        resp = self.client.post(reverse("accounts:password_change"), {
            "old_password": PASSWORD, "new_password1": "An0ther-Secure-Pass", "new_password2": "An0ther-Secure-Pass"})
        self.assertRedirects(resp, reverse("core:home"), fetch_redirect_response=False)
        user.profile.refresh_from_db()
        self.assertFalse(user.profile.must_change_password)
        self.assertEqual(self.client.get(reverse("core:employee_home")).status_code, 200)


class PermissionTests(HotelTestCase):
    ADMIN_URLS = ["accounts:user_list", "accounts:user_create", "accounts:department_list",
                  "accounts:department_create", "accounts:location_list", "accounts:location_create",
                  "core:settings", "core:audit_log"]

    def test_only_admin_reaches_administration(self):
        for role in (Role.IT_OFFICER, Role.MANAGER, Role.EMPLOYEE):
            self.login(self.make_user(f"u_{role}", role))
            for name in self.ADMIN_URLS:
                self.assertEqual(self.client.get(reverse(name)).status_code, 403, (role, name))
        self.login(self.make_user("adm", Role.ADMIN))
        for name in self.ADMIN_URLS:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_superuser_is_admin(self):
        su = User.objects.create_superuser("root", "r@example.invalid", PASSWORD)
        self.assertEqual(su.profile.role, Role.ADMIN)
        self.login(su)
        self.assertEqual(self.client.get(reverse("accounts:user_list")).status_code, 200)

    def test_employee_cannot_post_to_admin_actions(self):
        emp = self.make_user("emp")
        victim = self.make_user("victim")
        self.login(emp)
        self.assertEqual(self.client.post(reverse("accounts:user_toggle_active", args=[victim.pk])).status_code, 403)
        victim.refresh_from_db()
        self.assertTrue(victim.is_active)

    def test_employee_cannot_change_own_role_via_profile(self):
        emp = self.make_user("emp")
        self.login(emp)
        self.client.post(reverse("accounts:profile"), {"phone": "0700", "role": Role.ADMIN, "department": ""})
        emp.profile.refresh_from_db()
        self.assertEqual(emp.profile.role, Role.EMPLOYEE)
        self.assertEqual(emp.profile.phone, "0700")
        self.assertEqual(emp.profile.department, self.dept)


class UserAdministrationTests(HotelTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("adm", Role.ADMIN)
        self.login(self.admin)

    def test_create_user(self):
        resp = self.client.post(reverse("accounts:user_create"), {
            "username": "jdoe", "first_name": "Jane", "last_name": "Doe", "email": "j@example.invalid",
            "password1": "Temp-Passw0rd-123", "password2": "Temp-Passw0rd-123",
            "role": Role.EMPLOYEE, "department": self.dept.pk, "job_title": "Receptionist",
        })
        user = User.objects.get(username="jdoe")
        self.assertRedirects(resp, reverse("accounts:user_detail", args=[user.pk]))
        self.assertEqual(user.profile.department, self.dept)
        self.assertTrue(user.profile.must_change_password)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CREATE, object_id=str(user.pk)).exists())

    def test_create_user_validation_preserves_input(self):
        resp = self.client.post(reverse("accounts:user_create"), {
            "username": "ADM", "first_name": "Kept", "last_name": "Value", "password1": "short",
            "password2": "different", "role": Role.EMPLOYEE,
        })
        self.assertEqual(resp.status_code, 200)
        errors = resp.context["form"].errors
        for field in ("username", "password1", "password2", "department"):
            self.assertIn(field, errors)
        self.assertContains(resp, 'value="Kept"')

    def test_edit_user_records_changes(self):
        emp = self.make_user("emp")
        resp = self.client.post(reverse("accounts:user_edit", args=[emp.pk]), {
            "first_name": "E", "last_name": "Mp", "email": "", "is_active": "on",
            "role": Role.MANAGER, "department": self.other_dept.pk,
        })
        self.assertRedirects(resp, reverse("accounts:user_detail", args=[emp.pk]))
        emp.profile.refresh_from_db()
        self.assertEqual(emp.profile.role, Role.MANAGER)
        entry = AuditLog.objects.filter(action=AuditLog.Action.UPDATE, object_id=str(emp.pk)).latest("timestamp")
        self.assertEqual(entry.changes["role"], [Role.EMPLOYEE, Role.MANAGER])

    def test_admin_cannot_deactivate_or_demote_self(self):
        self.client.post(reverse("accounts:user_toggle_active", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        resp = self.client.post(reverse("accounts:user_edit", args=[self.admin.pk]), {
            "first_name": "A", "last_name": "B", "role": Role.EMPLOYEE, "department": self.dept.pk, "is_active": "on"})
        self.assertEqual(resp.status_code, 200)
        self.admin.profile.refresh_from_db()
        self.assertEqual(self.admin.profile.role, Role.ADMIN)

    def test_deactivate_and_reset_password(self):
        emp = self.make_user("emp")
        self.client.post(reverse("accounts:user_toggle_active", args=[emp.pk]))
        emp.refresh_from_db()
        self.assertFalse(emp.is_active)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DEACTIVATE, object_id=str(emp.pk)).exists())
        self.client.post(reverse("accounts:user_set_password", args=[emp.pk]), {
            "new_password1": "Reset-Passw0rd-99", "new_password2": "Reset-Passw0rd-99", "force_change": "on"})
        emp.refresh_from_db()
        self.assertTrue(emp.check_password("Reset-Passw0rd-99"))
        self.assertTrue(emp.profile.must_change_password)


class DepartmentLocationTests(HotelTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.make_user("adm", Role.ADMIN))

    def test_create_department_normalises_code_and_rejects_duplicates(self):
        self.client.post(reverse("accounts:department_create"), {"name": "Spa", "code": "spa", "is_active": "on"})
        self.assertTrue(Department.objects.filter(code="SPA").exists())
        resp = self.client.post(reverse("accounts:department_create"), {"name": "front office", "code": "FO2"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("name", resp.context["form"].errors)

    def test_deactivate_department_keeps_record(self):
        self.client.post(reverse("accounts:department_edit", args=[self.dept.pk]),
                         {"name": self.dept.name, "code": self.dept.code})
        self.dept.refresh_from_db()
        self.assertFalse(self.dept.is_active)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DEACTIVATE, object_id=str(self.dept.pk)).exists())
        resp = self.client.get(reverse("accounts:department_list"), {"status": "inactive"})
        self.assertContains(resp, self.dept.name)

    def test_create_location(self):
        self.client.post(reverse("accounts:location_create"),
                         {"name": "Lobby", "building": "Main", "floor": "G", "department": self.dept.pk, "is_active": "on"})
        self.assertTrue(Location.objects.filter(name="Lobby").exists())
        resp = self.client.post(reverse("accounts:location_create"), {"name": "lobby", "building": "main", "floor": "g"})
        self.assertEqual(resp.status_code, 200)
