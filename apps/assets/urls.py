from django.urls import path

from . import views

app_name = "assets"

urlpatterns = [
    path("", views.asset_list, name="list"),
    path("new/", views.asset_create, name="create"),
    path("export.<str:fmt>", views.asset_export, name="export"),
    path("labels/", views.asset_labels, name="labels"),
    path("scan/<uuid:token>/", views.asset_scan, name="scan"),
    path("<int:pk>/", views.asset_detail, name="detail"),
    path("<int:pk>/edit/", views.asset_edit, name="edit"),
    path("<int:pk>/status/", views.asset_status, name="status"),
    path("<int:pk>/attach/", views.asset_attach, name="attach"),
    path("<int:pk>/qr.png", views.asset_qr, name="qr"),
    path("files/<int:pk>/", views.attachment_view, name="attachment"),
    path("files/<int:pk>/remove/", views.attachment_hide, name="attachment_hide"),
    path("categories/", views.category_list, name="category_list"),
    path("categories/new/", views.category_edit, name="category_create"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/new/", views.supplier_edit, name="supplier_create"),
    path("suppliers/<int:pk>/edit/", views.supplier_edit, name="supplier_edit"),
]
