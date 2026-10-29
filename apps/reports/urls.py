from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.centre, name="centre"),
    path("analytics/", views.analytics_view, name="analytics"),
]
