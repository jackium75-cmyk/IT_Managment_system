from apps.accounts.roles import is_it_staff


def running_timer(request):
    """Expose the signed-in IT officer's running timer to every page (top bar)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not is_it_staff(user):
        return {}
    from .services import running_timer as get_running

    return {"running_timer": get_running(user)}
