from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("home/", views.employee_home, name="employee_home"),
    path("search/", views.search, name="search"),
    path("administration/settings/", views.system_settings, name="settings"),
    path("administration/audit-log/", views.audit_log, name="audit_log"),
]
