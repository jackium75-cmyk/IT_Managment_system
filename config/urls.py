from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("dashboard/", include("apps.dashboard.urls")),
    path("tasks/", include("apps.tasks.urls")),
    path("diary/", include("apps.diary.urls")),
    path("helpdesk/", include("apps.helpdesk.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("assets/", include("apps.assets.urls")),
    path("maintenance/", include("apps.maintenance.urls")),
    path("projects/", include("apps.projects.urls")),
    path("procurement/", include("apps.procurement.urls")),
    path("knowledge/", include("apps.knowledge.urls")),
    path("reports/", include("apps.reports.urls")),
    path("administration/backups/", include("apps.backups.urls")),
    # Django's built-in admin is kept for emergency maintenance by superusers.
    path(settings.DJANGO_ADMIN_URL, admin.site.urls),
]

handler403 = "apps.core.views.error_403"
handler404 = "apps.core.views.error_404"
handler500 = "apps.core.views.error_500"
