from django.urls import path

from . import views

app_name = "maintenance"

urlpatterns = [
    path("", views.record_list, name="list"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("compliance/", views.compliance_report, name="compliance"),
    path("plans/new/", views.plan_edit, name="plan_create"),
    path("plans/<int:pk>/edit/", views.plan_edit, name="plan_edit"),
    path("repairs/new/", views.corrective_create, name="corrective_create"),
    path("<int:pk>/", views.record_detail, name="record"),
    path("<int:pk>/complete/", views.record_complete, name="complete"),
    path("<int:pk>/reschedule/", views.record_reschedule, name="reschedule"),
    path("<int:pk>/cancel/", views.record_cancel, name="cancel"),
    path("<int:pk>/attach/", views.record_attach, name="attach"),
    path("<int:pk>/project/", views.record_set_project, name="set_project"),
    path("files/<int:pk>/", views.attachment_view, name="attachment"),
]
