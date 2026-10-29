"""
Role definitions. A user's role lives on ``UserProfile.role``; Django
superusers are always treated as System Administrators.
"""
from django.db import models


class Role(models.TextChoices):
    ADMIN = "admin", "System Administrator"
    IT_OFFICER = "it_officer", "IT Support Officer"
    MANAGER = "manager", "Department Head / Manager"
    EMPLOYEE = "employee", "Hotel Employee"


# Roles that perform IT work and see IT operational data.
IT_STAFF_ROLES = (Role.ADMIN, Role.IT_OFFICER)


def get_role(user):
    if user is None or not user.is_authenticated:
        return None
    if user.is_superuser:
        return Role.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile is not None else Role.EMPLOYEE


def has_role(user, *roles):
    return get_role(user) in roles


def is_admin(user):
    return has_role(user, Role.ADMIN)


def is_it_staff(user):
    return has_role(user, *IT_STAFF_ROLES)


def is_manager(user):
    return has_role(user, Role.MANAGER)
