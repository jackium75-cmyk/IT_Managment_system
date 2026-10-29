"""
Helpdesk business rules. Views call these; nothing else changes ticket state.
Each operation is one transaction that also writes status history, comments,
the IT officer's activity diary, the audit log and notifications.
"""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role, get_role, is_it_staff
from apps.core import audit
from apps.core.models import AuditLog, Sequence, SystemSetting
from apps.diary import timekeeping
from apps.diary.choices import ActivityType
from apps.diary.models import ActivityLog
from apps.evidence.models import EvidenceFile
from apps.notifications.models import Notification
from apps.notifications.services import admins, it_staff, notify
from apps.tasks.models import Priority

from .models import (
    EscalationRule, SLATarget, Ticket, TicketAttachment, TicketComment, TicketEscalation, TicketStatusHistory,
)

S = Ticket.Status

# How a requester's answers translate into priority (impact × urgency).
PRIORITY_MATRIX = {
    (Ticket.Impact.ONE, Ticket.Urgency.MINOR): Priority.LOW,
    (Ticket.Impact.ONE, Ticket.Urgency.SLOWED): Priority.MEDIUM,
    (Ticket.Impact.ONE, Ticket.Urgency.STOPPED): Priority.HIGH,
    (Ticket.Impact.SEVERAL, Ticket.Urgency.MINOR): Priority.MEDIUM,
    (Ticket.Impact.SEVERAL, Ticket.Urgency.SLOWED): Priority.HIGH,
    (Ticket.Impact.SEVERAL, Ticket.Urgency.STOPPED): Priority.HIGH,
    (Ticket.Impact.GUESTS, Ticket.Urgency.MINOR): Priority.MEDIUM,
    (Ticket.Impact.GUESTS, Ticket.Urgency.SLOWED): Priority.HIGH,
    (Ticket.Impact.GUESTS, Ticket.Urgency.STOPPED): Priority.CRITICAL,
}

DEFAULT_TARGETS = {  # minutes: (response, resolution) — used if an administrator deleted a row
    Priority.CRITICAL: (15, 4 * 60), Priority.HIGH: (30, 8 * 60),
    Priority.MEDIUM: (2 * 60, 24 * 60), Priority.LOW: (8 * 60, 72 * 60),
}

# Allowed status moves for IT staff. Reopening and requester confirmation have their own functions.
TRANSITIONS = {
    S.NEW: {S.ACKNOWLEDGED, S.IN_PROGRESS, S.WAITING, S.RESOLVED, S.CLOSED},
    S.ACKNOWLEDGED: {S.IN_PROGRESS, S.WAITING, S.RESOLVED, S.CLOSED},
    S.IN_PROGRESS: {S.WAITING, S.RESOLVED, S.CLOSED},
    S.WAITING: {S.IN_PROGRESS, S.RESOLVED, S.CLOSED},
    S.RESOLVED: {S.CLOSED},
    S.CLOSED: set(),
}


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def can_view(user, ticket):
    return Ticket.objects.visible_to(user).filter(pk=ticket.pk).exists()


def require_view(user, ticket):
    if not can_view(user, ticket):
        audit.log(AuditLog.Action.ACCESS_DENIED, ticket, actor=user, message="Attempted to open a ticket without permission")
        raise PermissionDenied("You do not have access to this ticket.")


def require_it(user, ticket=None):
    if not is_it_staff(user):
        audit.log(AuditLog.Action.ACCESS_DENIED, ticket, actor=user, message="Non-IT user attempted an IT-only ticket action")
        raise PermissionDenied("Only IT staff can do this.")


def is_requester(user, ticket):
    return ticket.requester_id == user.pk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def compute_priority(impact, urgency):
    return PRIORITY_MATRIX.get((impact, urgency), Priority.MEDIUM)


def targets_for(priority):
    row = SLATarget.objects.filter(priority=priority).first()
    if row:
        return row.response_minutes, row.resolution_minutes
    return DEFAULT_TARGETS[priority]


def set_due_dates(ticket, base=None):
    base = base or ticket.created_at or timezone.now()
    response, resolution = targets_for(ticket.priority)
    ticket.response_due_at = base + timedelta(minutes=response)
    ticket.resolution_due_at = base + timedelta(minutes=resolution + ticket.paused_minutes)


def _url(ticket):
    return reverse("helpdesk:detail", args=[ticket.pk])


def _history(ticket, old, new, user, note="", reopen=False):
    TicketStatusHistory.objects.create(ticket=ticket, from_status=old, to_status=new, changed_by=user, note=note,
                                       is_reopen=reopen)


def _system_comment(ticket, user, body):
    return TicketComment.objects.create(ticket=ticket, author=user, kind=TicketComment.Kind.SYSTEM, body=body)


def _diary(ticket, user, event, title, description="", outcome=""):
    """Diary lines are written only for IT staff (the diary records IT work)."""
    if user is None or not is_it_staff(user):
        return
    ActivityLog.objects.create(
        user=user, source=ActivityLog.Source.AUTO, event=event, activity_type=ActivityType.SUPPORT,
        occurred_at=timezone.now(), title=title[:200], description=description, outcome=outcome,
        department=ticket.department, location=ticket.location, ticket=ticket, created_by=user, asset=ticket.asset,
        project=ticket.project,
    )


def _mark_first_response(ticket, user):
    if ticket.first_response_at is None and is_it_staff(user):
        ticket.first_response_at = timezone.now()


def _notify_it_side(ticket, kind, title, body="", exclude=None, key=""):
    """The assignee if there is one, otherwise every IT staff member."""
    recipients = [ticket.assigned_to] if ticket.assigned_to_id else list(it_staff())
    notify(recipients, kind, title, body=body, url=_url(ticket), exclude=exclude, dedupe_key=key)


def _notify_requester(ticket, kind, title, body="", actor=None, key=""):
    notify([ticket.requester], kind, title, body=body, url=_url(ticket), exclude=actor, dedupe_key=key)


def _pause_end(ticket, now):
    if ticket.status == S.WAITING and ticket.waiting_since:
        paused = max(0, int((now - ticket.waiting_since).total_seconds() // 60))
        ticket.paused_minutes += paused
        if ticket.resolution_due_at:
            ticket.resolution_due_at += timedelta(minutes=paused)
        ticket.waiting_since = None
        ticket.waiting_on = ""


def _stop_ticket_timers(ticket):
    from .models import TicketTimeEntry

    for entry in TicketTimeEntry.objects.select_for_update(of=("self",)).filter(ticket=ticket, ended_at__isnull=True,
                                                                     is_voided=False):
        timekeeping.close_entry(entry, entry.user)


@transaction.atomic
def save_attachments(ticket, uploads, user, *, stage=TicketAttachment.Stage.REPORTED, is_internal=False,
                     comment=None, description=""):
    """``uploads`` are (file, ext, kind, content_type) tuples from evidence.validation.validate_many."""
    if is_internal and not is_it_staff(user):
        is_internal = False
    saved = []
    for f, _ext, kind, content_type in uploads:
        att = TicketAttachment(
            ticket=ticket, comment=comment, stage=stage, is_internal=is_internal, original_name=f.name[:255],
            content_type=content_type, kind=kind, size=f.size, sha256=EvidenceFile.fingerprint(f),
            description=description[:255], uploaded_by=user,
        )
        att.file.save(f.name, f, save=False)
        att.save()
        audit.log(AuditLog.Action.UPLOAD, ticket, actor=user,
                  message=f"Attachment uploaded: {att.original_name} ({att.size_display}){' [internal]' if is_internal else ''}")
        saved.append(att)
    return saved


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@transaction.atomic
def create_ticket(ticket, user, uploads=(), *, on_behalf_of=None):
    """Save a new Ticket. IT staff may log a ticket on behalf of an employee (phone / walk-up)."""
    requester = on_behalf_of or user
    if on_behalf_of is not None:
        require_it(user)
    ticket.requester = requester
    profile = requester.profile
    ticket.department = ticket.department or profile.department
    ticket.location = ticket.location or profile.location
    ticket.contact_phone = ticket.contact_phone or profile.phone
    ticket.contact_email = ticket.contact_email or requester.email
    if ticket.asset_id:
        ticket.affected_device = ticket.affected_device or f"{ticket.asset.tag} {ticket.asset.name}"[:150]
        ticket.location = ticket.location or ticket.asset.location
    if not ticket.priority_overridden:
        ticket.priority = compute_priority(ticket.impact, ticket.urgency)
    year = timezone.localdate().year
    prefix = "INC" if ticket.ticket_type == Ticket.Type.INCIDENT else "REQ"
    ticket.reference = f"{prefix}-{year}-{Sequence.next(prefix, year):05d}"
    ticket.status = S.NEW
    now = timezone.now()
    set_due_dates(ticket, now)
    ticket.full_clean(exclude=["created_at"])
    ticket.save()
    _history(ticket, "", S.NEW, user, "Reported" + (f" by {user.get_full_name() or user.username} on behalf of the requester"
                                                   if on_behalf_of else ""))
    if uploads:
        save_attachments(ticket, uploads, user, stage=TicketAttachment.Stage.REPORTED)
    audit.log(AuditLog.Action.CREATE, ticket, actor=user,
              message=f"{ticket.get_ticket_type_display().split(' (')[0]} reported, priority {ticket.get_priority_display()}")
    title = f"New {ticket.get_priority_display().lower()} ticket {ticket.reference}: {ticket.subject}"
    body = f"From {requester.get_full_name() or requester.username}" + (f" ({ticket.department})" if ticket.department else "")
    notify(it_staff(), Notification.Kind.TICKET_NEW, title, body=body, url=_url(ticket),
           dedupe_key=f"ticket-new-{ticket.pk}", exclude=user if is_it_staff(user) else None)
    if ticket.priority == Priority.CRITICAL:
        notify(list(it_staff()) + list(admins()), Notification.Kind.CRITICAL,
               f"CRITICAL: {ticket.reference} {ticket.subject}", body=body, url=_url(ticket),
               dedupe_key=f"ticket-critical-{ticket.pk}")
    return ticket


# ---------------------------------------------------------------------------
# IT actions
# ---------------------------------------------------------------------------
@transaction.atomic
def assign(ticket, assignee, user):
    require_it(user, ticket)
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    if assignee is not None and not is_it_staff(assignee):
        raise ValidationError({"assigned_to": "Tickets can only be assigned to IT staff."})
    old = ticket.assigned_to
    if old == assignee:
        return ticket
    ticket.assigned_to = assignee
    ticket.save(update_fields=["assigned_to", "updated_at"])
    who = (assignee.get_full_name() or assignee.username) if assignee else "nobody"
    _system_comment(ticket, user, f"Assigned to {who}.")
    audit.log(AuditLog.Action.ASSIGN, ticket, actor=user,
              changes={"assigned_to": [str(old.get_username()) if old else "", assignee.get_username() if assignee else ""]})
    if assignee is not None:
        notify([assignee], Notification.Kind.TICKET_ASSIGNED, f"Assigned to you: {ticket.reference} {ticket.subject}",
               url=_url(ticket), exclude=user, dedupe_key=f"ticket-assigned-{ticket.pk}-{assignee.pk}")
    return ticket


@transaction.atomic
def change_status(ticket, new_status, user, *, note="", waiting_on="", resolution_details="", root_cause="",
                  verification=""):
    require_it(user, ticket)
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    old = ticket.status
    note = (note or "").strip()
    if new_status not in TRANSITIONS.get(old, set()):
        raise ValidationError(f"A ticket cannot go from “{S(old).label}” to “{S(new_status).label}”.")
    if new_status == S.WAITING:
        if waiting_on not in Ticket.WaitingOn.values:
            raise ValidationError({"waiting_on": "Say what the ticket is waiting for."})
        if not note:
            raise ValidationError({"note": "Explain what is needed (the requester sees this if you are waiting on them)."})
    if new_status == S.RESOLVED and not (resolution_details or "").strip():
        raise ValidationError({"resolution_details": "Describe how the problem was resolved."})
    if new_status == S.CLOSED and old != S.RESOLVED and not note:
        raise ValidationError({"note": "Give a reason for closing without resolving (e.g. duplicate, not an IT issue)."})

    now = timezone.now()
    _pause_end(ticket, now)
    ticket.status = new_status
    if new_status == S.ACKNOWLEDGED:
        ticket.acknowledged_at = now
    if new_status in (S.ACKNOWLEDGED, S.IN_PROGRESS) and not ticket.acknowledged_at:
        ticket.acknowledged_at = now
    if new_status == S.WAITING:
        ticket.waiting_on, ticket.waiting_since = waiting_on, now
    if new_status == S.RESOLVED:
        ticket.resolved_at = now
        ticket.resolution_details = resolution_details.strip()
        ticket.root_cause = (root_cause or "").strip()
        ticket.verification = (verification or "").strip()
    if new_status == S.CLOSED:
        ticket.closed_at = now
        if old != S.RESOLVED and not ticket.resolution_details:
            ticket.resolution_details = note
    if new_status in (S.RESOLVED, S.CLOSED):
        _stop_ticket_timers(ticket)
    if ticket.assigned_to_id is None and new_status in (S.ACKNOWLEDGED, S.IN_PROGRESS):
        ticket.assigned_to = user
    _mark_first_response(ticket, user)
    ticket.save()

    history_note = note
    if new_status == S.WAITING:
        history_note = f"Waiting on {Ticket.WaitingOn(waiting_on).label.lower()}: {note}"
    _history(ticket, old, new_status, user, history_note)
    if new_status == S.WAITING and waiting_on == Ticket.WaitingOn.REQUESTER:
        TicketComment.objects.create(ticket=ticket, author=user, kind=TicketComment.Kind.PUBLIC, body=note,
                                     is_question=True)
        _notify_requester(ticket, Notification.Kind.TICKET_INFO, f"IT needs more information: {ticket.reference}",
                          body=note, actor=user)
    elif new_status == S.RESOLVED:
        _notify_requester(ticket, Notification.Kind.TICKET_RESOLVED,
                          f"Resolved: {ticket.reference} {ticket.subject}",
                          body=f"{ticket.resolution_details}\n\nPlease confirm the problem is fixed, or reopen the ticket.",
                          actor=user)
    else:
        _notify_requester(ticket, Notification.Kind.TICKET_STATUS,
                          f"{ticket.reference} is now {S(new_status).label.lower()}", body=history_note, actor=user,
                          key=f"ticket-status-{ticket.pk}-{new_status}")

    event = {S.ACKNOWLEDGED: ActivityLog.Event.TICKET_ACKNOWLEDGED, S.RESOLVED: ActivityLog.Event.TICKET_RESOLVED}.get(
        new_status, ActivityLog.Event.TICKET_STATUS)
    verb = {S.ACKNOWLEDGED: "Acknowledged", S.RESOLVED: "Resolved", S.CLOSED: "Closed",
            S.IN_PROGRESS: "Working on", S.WAITING: "Waiting on"}.get(new_status, "Updated")
    _diary(ticket, user, event, f"{verb} {ticket.reference}: {ticket.subject}", description=history_note,
           outcome=ticket.resolution_details if new_status == S.RESOLVED else "")
    audit.log(AuditLog.Action.STATUS_CHANGE, ticket, actor=user, changes={"status": [old, new_status]},
              message=history_note[:300])
    return ticket


@transaction.atomic
def set_asset(ticket, asset, user):
    """Link the ticket to the registered equipment it concerns (feeds the asset's service history)."""
    require_it(user, ticket)
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    old = ticket.asset
    if old == asset:
        return ticket
    ticket.asset = asset
    ticket.save(update_fields=["asset", "updated_at"])
    _system_comment(ticket, user, f"Linked to asset {asset}." if asset else "Asset link removed.")
    audit.log(AuditLog.Action.UPDATE, ticket, actor=user,
              changes={"asset": [old.tag if old else "", asset.tag if asset else ""]})
    return ticket


@transaction.atomic
def set_project(ticket, project, user):
    require_it(user, ticket)
    old = ticket.project
    if old == project:
        return ticket
    ticket.project = project
    ticket.save(update_fields=["project", "updated_at"])
    audit.log(AuditLog.Action.UPDATE, ticket, actor=user,
              changes={"project": [old.code if old else "", project.code if project else ""]})
    return ticket


@transaction.atomic
def change_priority(ticket, priority, user, reason):
    require_it(user, ticket)
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    reason = (reason or "").strip()
    if priority not in Priority.values:
        raise ValidationError({"priority": "Choose a priority."})
    if priority == ticket.priority:
        raise ValidationError({"priority": "That is already the priority."})
    if not reason:
        raise ValidationError({"reason": "Give a reason for changing the priority."})
    old = ticket.priority
    ticket.priority, ticket.priority_overridden = priority, True
    set_due_dates(ticket, ticket.created_at)
    ticket.save()
    _system_comment(ticket, user, f"Priority changed from {Priority(old).label} to {Priority(priority).label}: {reason}")
    audit.log(AuditLog.Action.UPDATE, ticket, actor=user, changes={"priority": [old, priority]}, message=reason)
    if priority == Priority.CRITICAL:
        notify(list(it_staff()) + list(admins()), Notification.Kind.CRITICAL,
               f"CRITICAL: {ticket.reference} {ticket.subject}", url=_url(ticket), exclude=user,
               dedupe_key=f"ticket-critical-{ticket.pk}")
    return ticket


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------
@transaction.atomic
def add_comment(ticket, user, body, *, kind=TicketComment.Kind.PUBLIC, uploads=(), stage=None, internal_files=False):
    require_view(user, ticket)
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    body = (body or "").strip()
    it = is_it_staff(user)
    if kind != TicketComment.Kind.PUBLIC and not it:
        raise PermissionDenied("Only IT staff can add internal notes.")
    if not body and not uploads:
        raise ValidationError({"body": "Write a message or attach a file."})
    if ticket.status == S.CLOSED and not it:
        raise ValidationError("This ticket is closed. Reopen it if the problem has come back.")
    comment = TicketComment.objects.create(ticket=ticket, author=user, kind=kind, body=body or "(attachment)")
    if uploads:
        internal = it and (kind in TicketComment.INTERNAL_KINDS or internal_files)
        save_attachments(ticket, uploads, user, comment=comment, is_internal=internal,
                         stage=stage or (TicketAttachment.Stage.TROUBLESHOOTING if it else TicketAttachment.Stage.REPORTED))

    if it:
        if kind == TicketComment.Kind.PUBLIC:
            _mark_first_response(ticket, user)
            ticket.save(update_fields=["first_response_at", "updated_at"])
            _notify_requester(ticket, Notification.Kind.TICKET_REPLY, f"IT replied on {ticket.reference}",
                              body=body[:500], actor=user)
        if kind == TicketComment.Kind.TROUBLESHOOTING:
            _diary(ticket, user, ActivityLog.Event.TICKET_TROUBLESHOOTING,
                   f"Troubleshooting {ticket.reference}: {ticket.subject}", description=body)
    else:
        if ticket.status == S.WAITING and ticket.waiting_on == Ticket.WaitingOn.REQUESTER:
            now = timezone.now()
            _pause_end(ticket, now)
            ticket.status = S.IN_PROGRESS
            ticket.save()
            _history(ticket, S.WAITING, S.IN_PROGRESS, user, "Requester replied")
        _notify_it_side(ticket, Notification.Kind.TICKET_REPLY,
                        f"{user.get_full_name() or user.username} replied on {ticket.reference}", body=body[:500],
                        exclude=user)
    audit.log(AuditLog.Action.UPDATE, ticket, actor=user, message=f"{TicketComment.Kind(kind).label} added")
    return comment


# ---------------------------------------------------------------------------
# Requester confirmation / reopening
# ---------------------------------------------------------------------------
@transaction.atomic
def confirm_resolution(ticket, user, *, rating=None, comment=""):
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    if not is_requester(user, ticket):
        raise PermissionDenied("Only the person who reported the problem can confirm it is fixed.")
    if ticket.status != S.RESOLVED:
        raise ValidationError("Only resolved tickets can be confirmed.")
    ticket.status, ticket.closed_at, ticket.confirmed_by_requester = S.CLOSED, timezone.now(), True
    ticket.feedback_rating = rating or None
    ticket.feedback_comment = (comment or "").strip()
    ticket.save()
    _history(ticket, S.RESOLVED, S.CLOSED, user, "Requester confirmed the problem is resolved"
             + (f" (rating {rating}/5)" if rating else ""))
    audit.log(AuditLog.Action.STATUS_CHANGE, ticket, actor=user, changes={"status": [S.RESOLVED, S.CLOSED]},
              message="Confirmed by requester")
    _notify_it_side(ticket, Notification.Kind.TICKET_STATUS, f"{ticket.reference} confirmed fixed by requester",
                    body=ticket.feedback_comment, exclude=user)
    return ticket


def can_reopen(user, ticket):
    if ticket.status not in (S.RESOLVED, S.CLOSED):
        return False
    if is_it_staff(user):
        return True
    if not is_requester(user, ticket):
        return False
    if ticket.status == S.CLOSED:
        days = SystemSetting.get().ticket_reopen_days
        return ticket.closed_at is not None and timezone.now() - ticket.closed_at <= timedelta(days=days)
    return True


@transaction.atomic
def reopen(ticket, user, reason):
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    reason = (reason or "").strip()
    if not can_reopen(user, ticket):
        raise PermissionDenied("This ticket can no longer be reopened. Please report a new problem.")
    if not reason:
        raise ValidationError({"reason": "Tell IT what is still wrong."})
    old = ticket.status
    now = timezone.now()
    ticket.status = S.IN_PROGRESS if ticket.assigned_to_id else S.NEW
    ticket.reopen_count += 1
    ticket.closed_at = None
    ticket.confirmed_by_requester = False
    _, resolution = targets_for(ticket.priority)
    ticket.resolution_due_at = now + timedelta(minutes=resolution)
    ticket.save()
    _history(ticket, old, ticket.status, user, f"Reopened: {reason}", reopen=True)
    TicketComment.objects.create(ticket=ticket, author=user, kind=TicketComment.Kind.PUBLIC,
                                 body=f"Reopened: {reason}")
    _diary(ticket, user, ActivityLog.Event.TICKET_REOPENED, f"Reopened {ticket.reference}: {ticket.subject}", reason)
    audit.log(AuditLog.Action.STATUS_CHANGE, ticket, actor=user, changes={"status": [old, ticket.status]},
              message=f"Reopened: {reason}"[:500])
    _notify_it_side(ticket, Notification.Kind.TICKET_REOPENED, f"Reopened: {ticket.reference} {ticket.subject}",
                    body=reason, exclude=user)
    return ticket


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
@transaction.atomic
def hide_attachment(attachment, user, reason):
    """Attachments are never destroyed; they are hidden with a reason (audited)."""
    require_it(user, attachment.ticket)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason for removing this file."})
    attachment.is_deleted, attachment.deleted_by, attachment.deleted_at = True, user, timezone.now()
    attachment.delete_reason = reason[:300]
    attachment.save()
    audit.log(AuditLog.Action.DELETE, attachment.ticket, actor=user,
              message=f"Attachment removed from view: {attachment.original_name}. Reason: {reason}"[:500])
    return attachment


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
@transaction.atomic
def start_timer(ticket, user):
    require_it(user, ticket)
    ticket = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
    if not ticket.is_open:
        raise ValidationError("Reopen the ticket before recording more work on it.")
    entry, stopped = timekeeping.start(user, ticket=ticket)
    if ticket.status in (S.NEW, S.ACKNOWLEDGED):
        old = ticket.status
        ticket.status = S.IN_PROGRESS
        ticket.acknowledged_at = ticket.acknowledged_at or timezone.now()
        _mark_first_response(ticket, user)
        if not ticket.assigned_to_id:
            ticket.assigned_to = user
        ticket.save()
        _history(ticket, old, S.IN_PROGRESS, user, "Work started")
        _diary(ticket, user, ActivityLog.Event.TICKET_STATUS, f"Working on {ticket.reference}: {ticket.subject}")
        _notify_requester(ticket, Notification.Kind.TICKET_STATUS, f"{ticket.reference} is now in progress",
                          actor=user, key=f"ticket-status-{ticket.pk}-in_progress")
    return entry, stopped


@transaction.atomic
def log_time(ticket, user, *, started_at, minutes, note=""):
    require_it(user, ticket)
    return timekeeping.record(user, started_at=started_at, minutes=minutes, note=note, ticket=ticket)


@transaction.atomic
def void_time(entry, user, reason):
    require_it(user, entry.ticket)
    if entry.user_id != user.pk and get_role(user) != Role.ADMIN:
        raise PermissionDenied("Only the person who recorded the time, or an administrator, can void it.")
    return timekeeping.void(entry, user, reason)


# ---------------------------------------------------------------------------
# Scheduled jobs: auto-close and escalation
# ---------------------------------------------------------------------------
def auto_close_resolved(now=None):
    now = now or timezone.now()
    days = SystemSetting.get().ticket_auto_close_days
    closed = 0
    for ticket in Ticket.objects.filter(status=S.RESOLVED, resolved_at__lt=now - timedelta(days=days)):
        with transaction.atomic():
            t = Ticket.objects.select_for_update(of=("self",)).get(pk=ticket.pk)
            if t.status != S.RESOLVED:
                continue
            t.status, t.closed_at = S.CLOSED, now
            t.save()
            _history(t, S.RESOLVED, S.CLOSED, None, f"Closed automatically: no response from requester within {days} days")
            audit.log(AuditLog.Action.STATUS_CHANGE, t, changes={"status": [S.RESOLVED, S.CLOSED]},
                      message="Auto-closed")
            closed += 1
    return closed


def _rule_matches(rule, ticket, now):
    if rule.priority_list and ticket.priority not in rule.priority_list:
        return False
    if rule.trigger == EscalationRule.Trigger.UNASSIGNED:
        return ticket.assigned_to_id is None and now - ticket.created_at >= timedelta(minutes=rule.threshold)
    if rule.trigger == EscalationRule.Trigger.RESPONSE_BREACH:
        return ticket.first_response_at is None and ticket.response_due_at and now > ticket.response_due_at
    if ticket.status == S.WAITING or not ticket.resolution_due_at:
        return False  # the clock is paused while waiting on others
    due = ticket.effective_resolution_due
    if rule.trigger == EscalationRule.Trigger.RESOLUTION_BREACH:
        return now > due
    if rule.trigger == EscalationRule.Trigger.RESOLUTION_WARNING:
        total = (due - ticket.created_at).total_seconds()
        used = (now - ticket.created_at).total_seconds()
        return total > 0 and used / total * 100 >= rule.threshold and now <= due
    return False


def _escalation_recipients(rule, ticket):
    people = []
    if rule.notify_assignee and ticket.assigned_to_id:
        people.append(ticket.assigned_to)
    if rule.notify_it_staff or (rule.notify_assignee and not ticket.assigned_to_id):
        people.extend(it_staff())
    if rule.notify_admins:
        people.extend(admins())
    if rule.notify_department_head and ticket.department_id and ticket.department.head_id:
        people.append(ticket.department.head)
    return people


def run_escalations(now=None):
    """Fire each matching active rule once per ticket (per reopen cycle). Returns number fired."""
    now = now or timezone.now()
    fired = 0
    rules = list(EscalationRule.objects.filter(is_active=True))
    if not rules:
        return 0
    for ticket in Ticket.objects.open().select_related("assigned_to", "department", "department__head"):
        for rule in rules:
            if not _rule_matches(rule, ticket, now):
                continue
            _, created = TicketEscalation.objects.get_or_create(ticket=ticket, rule=rule, cycle=ticket.reopen_count)
            if not created:
                continue
            notify(_escalation_recipients(rule, ticket), Notification.Kind.ESCALATION,
                   f"Escalation – {rule.name}: {ticket.reference} {ticket.subject}",
                   body=f"Priority {ticket.get_priority_display()}, status {ticket.get_status_display()}.",
                   url=_url(ticket), dedupe_key=f"esc-{rule.pk}-{ticket.pk}-{ticket.reopen_count}")
            audit.log(AuditLog.Action.OTHER, ticket, message=f"Escalation fired: {rule.name}")
            fired += 1
    return fired


def remind_overdue_tasks(today=None):
    """Once a day, tell each IT officer how many of their tasks are overdue."""
    from apps.tasks.models import Task

    today = today or timezone.localdate()
    sent = 0
    for officer in it_staff():
        overdue = Task.objects.overdue(today).filter(assigned_to=officer).count()
        due_today = Task.objects.open().filter(assigned_to=officer, planned_date=today).count()
        if overdue:
            sent += len(notify([officer], Notification.Kind.OVERDUE, f"You have {overdue} overdue task(s)",
                               url=reverse("tasks:list") + "?view=overdue", dedupe_key=f"overdue-{today}",
                               window_minutes=24 * 60))
        if due_today:
            sent += len(notify([officer], Notification.Kind.DEADLINE, f"{due_today} task(s) planned for today",
                               url=reverse("tasks:planner"), dedupe_key=f"due-{today}", window_minutes=24 * 60))
    return sent


def run_scheduled_jobs():
    return {"auto_closed": auto_close_resolved(), "escalations": run_escalations(),
            "task_reminders": remind_overdue_tasks()}


def ensure_jobs_ran():
    """Kept for existing callers; all scheduled jobs now live in apps.core.jobs."""
    from apps.core.jobs import ensure_jobs_ran as run

    run()
