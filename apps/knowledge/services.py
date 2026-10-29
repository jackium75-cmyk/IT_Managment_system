from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.roles import is_it_staff
from apps.assets.models import validate_no_secrets
from apps.core import audit
from apps.core.models import AuditLog
from apps.diary.choices import ActivityType
from apps.diary.models import ActivityLog

from .models import KnowledgeArticle

A = KnowledgeArticle.Status
TEXT_FIELDS = ["problem", "symptoms", "possible_causes", "diagnostics", "commands", "parts_replaced", "root_cause",
               "solution", "verification", "prevention"]


def require_it(user):
    if not is_it_staff(user):
        raise PermissionDenied("The knowledge base is for IT staff.")


def _check_secrets(article):
    errors = {}
    for f in TEXT_FIELDS:
        try:
            validate_no_secrets(getattr(article, f))
        except ValidationError as exc:
            errors[f] = exc.messages
    if errors:
        raise ValidationError(errors)


def from_ticket(ticket):
    """Unsaved article pre-filled from a resolved ticket (troubleshooting steps included)."""
    steps = "\n".join(f"- {c.body}" for c in ticket.comments.filter(kind="troubleshooting"))
    return KnowledgeArticle(
        title=ticket.subject, category=ticket.category, asset_category=ticket.asset.category if ticket.asset_id else None,
        problem=ticket.description, diagnostics=steps, root_cause=ticket.root_cause,
        solution=ticket.resolution_details, verification=ticket.verification, source_ticket=ticket)


@transaction.atomic
def save_article(article, user, *, created):
    require_it(user)
    _check_secrets(article)
    if created:
        article.author = user
    article.full_clean()
    article.save()
    if created and article.source_ticket_id:
        article.used_in_tickets.add(article.source_ticket)
        if article.source_ticket.asset_id:
            article.related_assets.add(article.source_ticket.asset)
    audit.log(AuditLog.Action.CREATE if created else AuditLog.Action.UPDATE, article, actor=user,
              message=f"Knowledge article {'written' if created else 'updated'} ({article.get_status_display()})")
    if created:
        ActivityLog.objects.create(
            user=user, source=ActivityLog.Source.AUTO, event=ActivityLog.Event.KNOWLEDGE,
            activity_type=ActivityType.DOCUMENTATION, occurred_at=timezone.now(),
            title=f"Documented solution: {article.title}"[:200], ticket=article.source_ticket, created_by=user)
    return article


@transaction.atomic
def set_verified(article, user, verified):
    require_it(user)
    article.is_verified = verified
    article.verified_by = user if verified else None
    article.verified_at = timezone.now() if verified else None
    article.save(update_fields=["is_verified", "verified_by", "verified_at", "updated_at"])
    audit.log(AuditLog.Action.UPDATE, article, actor=user, message="Marked verified" if verified else "Marked unverified")
    return article


@transaction.atomic
def apply_to_ticket(article, ticket, user, note=""):
    """Record that this solution was used on a ticket (adds an internal note)."""
    require_it(user)
    if article.status != A.PUBLISHED:
        raise ValidationError("Only published articles can be applied.")
    from apps.helpdesk.models import TicketComment

    article.used_in_tickets.add(ticket)
    if ticket.asset_id:
        article.related_assets.add(ticket.asset)
    body = f"Applied knowledge article “{article.title}” ({'verified' if article.is_verified else 'unverified'})."
    if note:
        body += f" {note}"
    TicketComment.objects.create(ticket=ticket, author=user, kind=TicketComment.Kind.INTERNAL, body=body)
    audit.log(AuditLog.Action.UPDATE, ticket, actor=user, message=f"Knowledge article applied: {article.title}"[:500])
