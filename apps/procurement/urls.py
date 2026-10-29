from django.urls import path

from . import views

app_name = "procurement"

urlpatterns = [
    path("", views.request_list, name="list"),
    path("new/", views.request_edit, name="create"),
    path("<int:pk>/", views.request_detail, name="detail"),
    path("<int:pk>/edit/", views.request_edit, name="edit"),
    path("<int:pk>/submit/", views.request_submit, name="submit"),
    path("<int:pk>/decide/", views.request_decide, name="decide"),
    path("<int:pk>/ordered/", views.request_ordered, name="ordered"),
    path("<int:pk>/delivered/", views.request_delivered, name="delivered"),
    path("<int:pk>/cancel/", views.request_cancel, name="cancel"),
    path("<int:pk>/attach/", views.attach, name="attach"),
    path("files/<int:pk>/", views.attachment_view, name="attachment"),
]
