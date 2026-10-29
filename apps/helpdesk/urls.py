from django.urls import path

from . import views

app_name = "helpdesk"

urlpatterns = [
    path("", views.ticket_list, name="list"),
    path("new/", views.ticket_create, name="create"),
    path("<int:pk>/", views.ticket_detail, name="detail"),
    path("<int:pk>/comment/", views.ticket_comment, name="comment"),
    path("<int:pk>/status/", views.ticket_status, name="status"),
    path("<int:pk>/priority/", views.ticket_priority, name="priority"),
    path("<int:pk>/assign/", views.ticket_assign, name="assign"),
    path("<int:pk>/asset/", views.ticket_set_asset, name="set_asset"),
    path("<int:pk>/project/", views.ticket_set_project, name="set_project"),
    path("<int:pk>/confirm/", views.ticket_confirm, name="confirm"),
    path("<int:pk>/reopen/", views.ticket_reopen, name="reopen"),
    path("<int:pk>/timer/start/", views.ticket_timer_start, name="timer_start"),
    path("<int:pk>/time/", views.ticket_log_time, name="log_time"),
    path("<int:pk>/task/", views.ticket_create_task, name="create_task"),
    path("time/<int:entry_pk>/void/", views.time_void, name="time_void"),
    path("files/<int:pk>/", views.attachment_view, name="attachment"),
    path("files/<int:pk>/remove/", views.attachment_hide, name="attachment_hide"),
    path("settings/categories/", views.category_list, name="category_list"),
    path("settings/categories/new/", views.category_edit, name="category_create"),
    path("settings/categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
    path("settings/targets/", views.sla_list, name="sla_list"),
    path("settings/escalation/new/", views.rule_edit, name="rule_create"),
    path("settings/escalation/<int:pk>/edit/", views.rule_edit, name="rule_edit"),
]
