from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import audit
from .models import AuditLog


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit.log(AuditLog.Action.LOGIN, user, actor=user, request=request, message="Signed in")


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        audit.log(AuditLog.Action.LOGOUT, user, actor=user, request=request, message="Signed out")


@receiver(post_save, sender=LogEntry)
def on_django_admin_change(sender, instance, created, **kwargs):
    """Changes made in Django's built-in admin site bypass the application's services, so record them too."""
    if not created:
        return
    action = {ADDITION: AuditLog.Action.CREATE, CHANGE: AuditLog.Action.UPDATE,
              DELETION: AuditLog.Action.DELETE}.get(instance.action_flag, AuditLog.Action.OTHER)
    model = instance.content_type.model_class() if instance.content_type_id else None
    label = model._meta.verbose_name.capitalize() if model else "Record"
    audit.log(action, actor=instance.user,
              object_repr=f"{label}: {instance.object_repr}",
              message=f"Via Django admin site: {instance.get_change_message() or instance.get_action_flag_display()}")
