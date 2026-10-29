from django.urls import path

from . import views

app_name = "knowledge"

urlpatterns = [
    path("", views.article_list, name="list"),
    path("new/", views.article_edit, name="create"),
    path("<int:pk>/", views.article_detail, name="detail"),
    path("<int:pk>/edit/", views.article_edit, name="edit"),
    path("<int:pk>/verify/", views.article_verify, name="verify"),
    path("<int:pk>/apply/<int:ticket_pk>/", views.apply_to_ticket, name="apply"),
]
