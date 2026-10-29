from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import Asset, AssetCategory, Supplier


@admin.register(Asset)
class AssetAdmin(ReadOnlyAdmin):
    list_display = ("tag", "name", "category", "status", "location", "assigned_to")
    list_filter = ("status", "category")
    search_fields = ("tag", "name", "serial_number", "hostname")
    readonly_fields = ("tag", "qr_token")


admin.site.register(AssetCategory)
admin.site.register(Supplier)
