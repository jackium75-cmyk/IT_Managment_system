"""
Dashboard metric registry.

Each module registers *providers*: functions that receive a ``DashboardFilters``
object and return cards or charts computed from real database records. Later
phases (tasks, tickets, assets, ...) add their own providers here without
changing the dashboard view.

Card dict:  {"label", "value", "icon", "tone", "url", "help"}
Chart dict: {"id", "title", "type", "labels", "datasets": [{"label", "data"}], "help"}
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time

from django.utils import timezone

_card_providers = []
_chart_providers = []


@dataclass
class DashboardFilters:
    user: object
    start: date
    end: date
    department_id: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def start_dt(self):
        return timezone.make_aware(datetime.combine(self.start, time.min), timezone.get_current_timezone())

    @property
    def end_dt(self):
        return timezone.make_aware(datetime.combine(self.end, time.max), timezone.get_current_timezone())

    def days(self):
        current = self.start
        out = []
        while current <= self.end:
            out.append(current)
            current = date.fromordinal(current.toordinal() + 1)
        return out


def card_provider(section, order=100, roles=None):
    def decorator(func):
        _card_providers.append({"section": section, "order": order, "roles": roles, "func": func})
        return func
    return decorator


def chart_provider(order=100, roles=None):
    def decorator(func):
        _chart_providers.append({"order": order, "roles": roles, "func": func})
        return func
    return decorator


def _allowed(entry, role):
    return entry["roles"] is None or role in entry["roles"]


def build_cards(filters, role):
    sections = {}
    for entry in sorted(_card_providers, key=lambda e: e["order"]):
        if _allowed(entry, role):
            sections.setdefault(entry["section"], []).extend(entry["func"](filters))
    return sections


def build_charts(filters, role):
    charts = []
    for entry in sorted(_chart_providers, key=lambda e: e["order"]):
        if _allowed(entry, role):
            charts.extend(entry["func"](filters))
    return charts
