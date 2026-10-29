from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.roles import Role
from apps.core.permissions import role_required

from . import services
from .models import BackupRecord

admin_required = role_required(Role.ADMIN)


@admin_required
def backup_list(request):
    services.expire_stale_runs()
    services.sync_records()
    page = Paginator(BackupRecord.objects.select_related("triggered_by"), 25).get_page(request.GET.get("page"))
    running = BackupRecord.objects.filter(status=BackupRecord.Status.RUNNING).exists() or \
        BackupRecord.objects.filter(verify_status=BackupRecord.Verify.RUNNING).exists()
    return render(request, "backups/list.html", {"page_obj": page, "h": services.health(), "running": running})


@admin_required
@require_POST
def backup_run(request):
    if services.is_busy():
        messages.warning(request, "A backup or verification is already running. Refresh this page in a minute.")
    else:
        services.in_background(services.run_backup, BackupRecord.Kind.MANUAL, request.user, verify=True)
        messages.success(request, "Backup started. It is verified automatically when it finishes; this page "
                                  "refreshes until then.")
    return redirect("backups:list")


@admin_required
@require_POST
def backup_verify(request, pk):
    record = get_object_or_404(BackupRecord, pk=pk, status=BackupRecord.Status.SUCCESS)
    if services.is_busy():
        messages.warning(request, "A backup or verification is already running. Try again when it has finished.")
    else:
        services.in_background(services.run_verify, record, request.user)
        messages.success(request, f"Verification of {record.file_name} started.")
    return redirect("backups:list")
