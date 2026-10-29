from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile
from .roles import Role


@receiver(post_save, sender=get_user_model())
def ensure_profile(sender, instance, created, **kwargs):
    """Every user gets a profile; superusers created from the command line become administrators."""
    if created:
        UserProfile.objects.get_or_create(
            user=instance, defaults={"role": Role.ADMIN if instance.is_superuser else Role.EMPLOYEE}
        )
