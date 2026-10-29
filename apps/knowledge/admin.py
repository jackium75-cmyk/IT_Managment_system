from django.contrib import admin

from apps.core.admin import ReadOnlyAdmin

from .models import KnowledgeArticle


@admin.register(KnowledgeArticle)
class KnowledgeArticleAdmin(ReadOnlyAdmin):
    list_display = ("title", "status", "is_verified", "author", "updated_at")
    list_filter = ("status", "is_verified")
