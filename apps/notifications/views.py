from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import Notification


@login_required
def notification_list(request):
    qs = Notification.objects.filter(recipient=request.user)
    show = request.GET.get("show", "unread")
    if show == "unread":
        qs = qs.filter(is_read=False)
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {"page_obj": page, "show": show})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)  # never another user's
    if not n.is_read:
        n.is_read, n.read_at = True, timezone.now()
        n.save(update_fields=["is_read", "read_at"])
    if n.url and url_has_allowed_host_and_scheme(n.url, allowed_hosts={request.get_host()}):
        return redirect(n.url)
    return redirect("notifications:list")


@login_required
@require_POST
def mark_all_read(request):
    count = Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True, read_at=timezone.now())
    messages.success(request, f"{count} notification(s) marked as read.")
    return redirect("notifications:list")
