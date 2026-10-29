def unread_notifications(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from .services import unread_count

    return {"unread_notifications": unread_count(user)}
