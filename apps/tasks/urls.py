from django.urls import path

from . import views

app_name = "tasks"

urlpatterns = [
    path("", views.task_list, name="list"),
    path("new/", views.task_create, name="create"),
    path("unplanned/new/", views.unplanned_create, name="unplanned_create"),
    path("<int:pk>/", views.task_detail, name="detail"),
    path("<int:pk>/edit/", views.task_edit, name="edit"),
    path("<int:pk>/status/", views.task_status, name="status"),
    path("<int:pk>/complete/", views.task_complete, name="complete"),
    path("<int:pk>/reschedule/", views.task_reschedule, name="reschedule"),
    path("<int:pk>/comment/", views.task_comment, name="comment"),
    path("<int:pk>/time/", views.task_log_time, name="log_time"),
    path("<int:pk>/timer/start/", views.timer_start, name="timer_start"),
    path("timer/stop/", views.timer_stop, name="timer_stop"),
    path("time/<int:entry_pk>/void/", views.time_void, name="time_void"),
    path("<int:pk>/attach/", views.task_attach, name="attach"),
    path("<int:pk>/link-ticket/", views.task_link_ticket, name="link_ticket"),
    path("files/<int:pk>/", views.task_attachment, name="attachment"),
    path("files/<int:pk>/remove/", views.task_attachment_hide, name="attachment_hide"),
    path("planner/", views.planner_day, name="planner"),
    path("planner/week/", views.planner_week, name="planner_week"),
    path("planner/carry-over/", views.planner_carry_over, name="carry_over"),
    path("recurring/", views.recurring_list, name="recurring_list"),
    path("recurring/new/", views.recurring_edit, name="recurring_create"),
    path("recurring/<int:pk>/edit/", views.recurring_edit, name="recurring_edit"),
    path("categories/", views.category_list, name="category_list"),
    path("categories/new/", views.category_edit, name="category_create"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
]
