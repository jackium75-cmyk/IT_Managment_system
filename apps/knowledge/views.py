from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.roles import IT_STAFF_ROLES
from apps.assets.models import AssetCategory
from apps.core.permissions import role_required
from apps.helpdesk.models import Ticket, TicketCategory
from apps.tasks.views import _apply_errors

from . import matching, services
from .forms import ArticleForm
from .models import KnowledgeArticle

it_required = role_required(*IT_STAFF_ROLES)
A = KnowledgeArticle.Status


@it_required
def article_list(request):
    status = request.GET.get("status", "published")
    qs = KnowledgeArticle.objects.select_related("category", "asset_category", "author")
    if status in A.values:
        qs = qs.filter(status=status)
    f = {k: request.GET.get(k, "").strip() for k in ("q", "category", "device", "verified")}
    if f["category"].isdigit():
        qs = qs.filter(category_id=f["category"])
    if f["device"].isdigit():
        qs = qs.filter(asset_category_id=f["device"])
    if f["verified"] in ("1", "0"):
        qs = qs.filter(is_verified=f["verified"] == "1")
    articles = matching.search(f["q"], qs) if f["q"] else list(qs)
    page = Paginator(articles, 25).get_page(request.GET.get("page"))
    return render(request, "knowledge/article_list.html", {
        "page_obj": page, "status": status, "f": f, "statuses": A.choices,
        "categories": TicketCategory.objects.filter(parent__isnull=True), "devices": AssetCategory.objects.all()})


@it_required
def article_detail(request, pk):
    article = get_object_or_404(KnowledgeArticle.objects.select_related(
        "category", "asset_category", "author", "verified_by", "source_ticket"), pk=pk)
    return render(request, "knowledge/article_detail.html", {
        "article": article, "used_in": article.used_in_tickets.order_by("-created_at")[:20],
        "assets": article.related_assets.all()[:20]})


@it_required
def article_edit(request, pk=None):
    article = get_object_or_404(KnowledgeArticle, pk=pk) if pk else None
    source = None
    if not article and (request.GET.get("ticket") or request.POST.get("source_ticket") or "").isdigit():
        source = get_object_or_404(Ticket, pk=request.GET.get("ticket") or request.POST.get("source_ticket"))
    if request.method == "POST":
        form = ArticleForm(request.POST, instance=article or (services.from_ticket(source) if source else None))
        if form.is_valid():
            obj = form.save(commit=False)
            if source:
                obj.source_ticket = source
            try:
                services.save_article(obj, request.user, created=article is None)
            except ValidationError as exc:
                _apply_errors(form, exc)
            else:
                messages.success(request, f"Article {'saved' if article else 'created'} ({obj.get_status_display().lower()}).")
                return redirect(obj)
    else:
        form = ArticleForm(instance=article or (services.from_ticket(source) if source else None),
                           initial=None if article else {"status": A.PUBLISHED if source else A.DRAFT})
    return render(request, "knowledge/article_form.html", {"form": form, "article": article, "source": source})


@it_required
@require_POST
def article_verify(request, pk):
    article = get_object_or_404(KnowledgeArticle, pk=pk)
    verified = request.POST.get("verified") == "1"
    services.set_verified(article, request.user, verified)
    messages.success(request, "Marked as verified." if verified else "Marked as unverified.")
    return redirect(article)


@it_required
@require_POST
def apply_to_ticket(request, pk, ticket_pk):
    article = get_object_or_404(KnowledgeArticle, pk=pk)
    ticket = get_object_or_404(Ticket, pk=ticket_pk)
    try:
        services.apply_to_ticket(article, ticket, request.user, request.POST.get("note", "").strip())
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Recorded on the ticket as an internal note. Check the fix worked before resolving.")
    return redirect(ticket)
