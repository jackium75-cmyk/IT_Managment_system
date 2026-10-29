"""Phase 7 security review: regression tests that sweep the whole application."""
import re
from pathlib import Path

from django.conf import settings
from django.test import RequestFactory
from django.http import HttpResponse
from django.urls import URLResolver, get_resolver, reverse

from apps.accounts.roles import Role

from .middleware import SecurityHeadersMiddleware
from .testing import HotelTestCase

# Pages that are reachable without signing in, on purpose.
PUBLIC = {"accounts:login"}
# Areas an ordinary hotel employee must never be able to open.
STAFF_ONLY_PREFIXES = ("/dashboard/", "/tasks/", "/diary/", "/assets/", "/maintenance/", "/projects/",
                       "/procurement/", "/knowledge/", "/reports/", "/administration/", "/accounts/users/",
                       "/accounts/departments/", "/accounts/locations/", "/helpdesk/settings/")
ADMIN_ONLY_PREFIXES = ("/administration/", "/accounts/users/", "/accounts/departments/", "/accounts/locations/",
                       "/helpdesk/settings/")


def all_urls():
    """Every URL in the project with sample arguments filled in: (url, url name)."""

    def walk(patterns, prefix="", namespace=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                ns = f"{namespace}{p.namespace}:" if p.namespace else namespace
                yield from walk(p.url_patterns, prefix + str(p.pattern), ns)
            else:
                yield prefix + str(p.pattern), f"{namespace}{p.name}"

    for route, name in walk(get_resolver().url_patterns):
        if route.startswith(settings.DJANGO_ADMIN_URL) or "(?P" in route:
            continue
        url = re.sub(r"<int:\w+>", "1", route)
        url = re.sub(r"<(?:str|slug|uuid|path):\w+>", "x", url)
        yield "/" + url, name


class AccessSweepTests(HotelTestCase):
    def test_every_page_requires_sign_in(self):
        checked = 0
        for url, name in all_urls():
            if name in PUBLIC:
                continue
            for method in (self.client.get, self.client.post):
                resp = method(url)
                ok = (resp.status_code == 302 and reverse("accounts:login") in resp["Location"]) or \
                    resp.status_code in (403, 404, 405)
                self.assertTrue(ok, f"{method.__name__.upper()} {url} ({name}) returned {resp.status_code} "
                                    "to an anonymous visitor")
                checked += 1
        self.assertGreater(checked, 250)

    def test_employees_cannot_reach_staff_areas(self):
        self.login(self.make_user("rose"))
        for url, name in all_urls():
            if url.startswith(STAFF_ONLY_PREFIXES):
                for method in (self.client.get, self.client.post):
                    code = method(url).status_code
                    self.assertIn(code, (403, 404, 405), f"employee {method.__name__.upper()} {url} -> {code}")

    def test_it_staff_and_managers_cannot_reach_administration(self):
        for role in (Role.IT_OFFICER, Role.MANAGER):
            self.login(self.make_user(f"u_{role}", role))
            for url, name in all_urls():
                if url.startswith(ADMIN_ONLY_PREFIXES):
                    code = self.client.get(url).status_code
                    self.assertIn(code, (403, 404, 405), f"{role} GET {url} -> {code}")

    def test_denied_access_is_audited(self):
        from .models import AuditLog

        self.login(self.make_user("rose"))
        self.client.get("/administration/backups/")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.ACCESS_DENIED).exists())


class HeaderTests(HotelTestCase):
    def test_security_headers_on_pages(self):
        self.login(self.make_user("ian", Role.IT_OFFICER))
        resp = self.client.get(reverse("dashboard:index"))
        csp = resp["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertEqual(resp["Cache-Control"], "private, no-store")
        self.assertEqual(resp["X-Frame-Options"], "DENY")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertIn("geolocation=()", resp["Permissions-Policy"])

    def test_views_can_set_a_stricter_policy(self):
        def view(request):
            r = HttpResponse("file")
            r["Content-Security-Policy"] = "default-src 'none'; sandbox"
            r["Cache-Control"] = "private, max-age=3600"
            return r

        request = RequestFactory().get("/")
        request.user = self.make_user("ian", Role.IT_OFFICER)
        resp = SecurityHeadersMiddleware(view)(request)
        self.assertEqual(resp["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(resp["Cache-Control"], "private, max-age=3600")

    def test_session_cookie_flags(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)


class TemplateHygieneTests(HotelTestCase):
    """The CSP forbids inline scripts, so templates must not rely on them (and must not load third-party code)."""

    def templates(self):
        return sorted(Path(settings.BASE_DIR, "templates").rglob("*.html"))

    def test_no_inline_scripts_or_event_handlers(self):
        for path in self.templates():
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"<script\b([^>]*)>", text):
                attrs = m.group(1)
                self.assertTrue("src=" in attrs or "application/json" in attrs, f"inline <script> in {path.name}")
            self.assertIsNone(re.search(r"\son[a-z]+\s*=\s*[\"']", text), f"inline event handler in {path.name}")
            self.assertNotIn("javascript:", text, path.name)

    def test_no_third_party_resources(self):
        for path in self.templates():
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"""(src|href)=["']https?://""", text), f"external resource in {path.name}")


class SecretsTests(HotelTestCase):
    def test_settings_do_not_contain_real_secrets(self):
        text = Path(settings.BASE_DIR, "config", "settings.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"PASSWORD\s*=\s*[\"'][^\"']+[\"']")
        example = Path(settings.BASE_DIR, ".env.example").read_text(encoding="utf-8")
        self.assertRegex(example, r"(?m)^DJANGO_SECRET_KEY=$")
        self.assertRegex(example, r"(?m)^DB_PASSWORD=$")
        self.assertRegex(example, r"(?m)^DJANGO_DEBUG=0$")


class HousekeepingTests(HotelTestCase):
    def test_old_lockout_counters_and_expired_sessions_are_removed(self):
        from datetime import timedelta

        from django.contrib.sessions.models import Session
        from django.utils import timezone

        from apps.accounts.models import LoginAttempt

        from .jobs import housekeeping
        from .models import AuditLog

        old = LoginAttempt.objects.create(username="x", ip_address="10.0.0.1", successful=False)
        LoginAttempt.objects.filter(pk=old.pk).update(timestamp=timezone.now() - timedelta(days=91))
        recent = LoginAttempt.objects.create(username="x", ip_address="10.0.0.1", successful=False)
        Session.objects.create(session_key="a" * 32, session_data="", expire_date=timezone.now() - timedelta(hours=1))
        Session.objects.create(session_key="b" * 32, session_data="", expire_date=timezone.now() + timedelta(hours=1))
        audit_before = AuditLog.objects.count()
        result = housekeeping()
        self.assertEqual((result["old_login_attempts_removed"], result["expired_sessions_removed"]), (1, 1))
        self.assertEqual(list(LoginAttempt.objects.values_list("pk", flat=True)), [recent.pk])
        self.assertEqual(Session.objects.get().session_key, "b" * 32)
        self.assertEqual(AuditLog.objects.count(), audit_before)  # the audit log is never pruned


class DjangoAdminTests(HotelTestCase):
    """The built-in admin site is for emergencies: business records are view-only and every change is audited."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.root = get_user_model().objects.create_superuser("root", password="Str0ng-Test-Passw0rd!")
        self.login(self.root)
        self.base = "/" + settings.DJANGO_ADMIN_URL

    def test_business_records_are_view_only(self):
        from django.contrib import admin

        from apps.helpdesk.models import Ticket
        from apps.tasks.models import Task

        request = RequestFactory().get("/")
        request.user = self.root
        for model in (Ticket, Task):
            model_admin = admin.site._registry[model]
            self.assertTrue(model_admin.has_view_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
        self.assertEqual(self.client.get(f"{self.base}helpdesk/ticket/").status_code, 200)
        self.assertEqual(self.client.get(f"{self.base}helpdesk/ticket/add/").status_code, 403)

    def test_changes_through_admin_site_are_audited(self):
        from .models import AuditLog

        resp = self.client.post(f"{self.base}accounts/department/{self.dept.pk}/change/",
                                {"name": "Front Office (renamed)", "code": "FO", "is_active": "on"})
        self.assertEqual(resp.status_code, 302)
        entry = AuditLog.objects.filter(action=AuditLog.Action.UPDATE, message__startswith="Via Django admin").get()
        self.assertEqual(entry.actor, self.root)
        self.assertIn("Front Office (renamed)", entry.object_repr)
