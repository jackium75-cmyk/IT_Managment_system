from django.urls import path

from . import views

app_name = "diary"

urlpatterns = [
    path("", views.diary_list, name="list"),
    path("new/", views.entry_create, name="create"),
    path("<int:pk>/", views.entry_detail, name="detail"),
    path("<int:pk>/edit/", views.entry_edit, name="edit"),
    path("<int:pk>/void/", views.entry_void, name="void"),
]
