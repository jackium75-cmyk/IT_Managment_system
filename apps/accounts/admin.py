from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import Department, Location, LoginAttempt, UserProfile


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "head", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "building", "floor", "department", "is_active")
    list_filter = ("is_active", "department")
    search_fields = ("name", "building")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "department", "job_title")
    list_filter = ("role", "department")
    search_fields = ("user__username", "user__first_name", "user__last_name")


@admin.register(LoginAttempt)
class LoginAttemptAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "username", "ip_address", "successful")
    list_filter = ("successful",)
    search_fields = ("username", "ip_address")
