from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_edit, name="create"),
    path("<int:pk>/", views.project_detail, name="detail"),
    path("<int:pk>/edit/", views.project_edit, name="edit"),
    path("<int:pk>/report/", views.project_report, name="report"),
    path("<int:pk>/confirm-benefits/", views.confirm_benefits, name="confirm_benefits"),
    path("<int:pk>/milestones/", views.milestone_add, name="milestone_add"),
    path("milestones/<int:pk>/toggle/", views.milestone_toggle, name="milestone_toggle"),
    path("<int:pk>/updates/", views.update_add, name="update_add"),
    path("<int:pk>/attach/", views.attach, name="attach"),
    path("files/<int:pk>/", views.attachment_view, name="attachment"),
]
