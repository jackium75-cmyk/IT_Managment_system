"""Shared helpers for automated tests."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from apps.accounts.models import Department
from apps.accounts.roles import Role

PASSWORD = "Str0ng-Test-Passw0rd!"


class HotelTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.dept, _ = Department.objects.get_or_create(code="FO", defaults={"name": "Front Office"})
        self.other_dept, _ = Department.objects.get_or_create(code="FNB", defaults={"name": "Food & Beverage"})

    def make_user(self, username, role=Role.EMPLOYEE, department=None, **extra):
        user = get_user_model().objects.create_user(username=username, password=PASSWORD, **extra)
        profile = user.profile
        profile.role = role
        profile.department = department if department is not None else self.dept
        profile.save()
        return user

    def login(self, user):
        self.client.force_login(user)
