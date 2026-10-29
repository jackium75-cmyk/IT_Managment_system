"""
Format-independent report structure. Builders produce a ``Report``; renderers
turn it into HTML, PDF, Excel or CSV, so every format shows identical figures.
"""
from dataclasses import dataclass, field
from datetime import datetime

BASIS = {
    "recorded": ("Recorded", "Counted from records in the system."),
    "reported": ("Reported by IT", "Entered by IT staff (time logs, notes, benefits); not independently checked."),
    "estimated": ("Estimated", "Planned or estimated values, not actual results."),
    "verified": ("Verified", "Confirmed by someone other than IT (requester confirmation, manager confirmation)."),
}


@dataclass
class Metric:
    label: str
    value: object
    basis: str = "recorded"
    help: str = ""


@dataclass
class Section:
    title: str
    kind: str  # "metrics" | "table" | "text" | "chart"
    metrics: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    paragraphs: list = field(default_factory=list)
    chart: dict | None = None
    note: str = ""
    empty: str = "Nothing recorded for this period."
    links: list = field(default_factory=list)  # optional URL per row (HTML only)
    it_only: bool = False


@dataclass
class Report:
    kind: str
    title: str
    period_label: str
    filters: str
    generated_at: datetime
    generated_by: str
    hotel_name: str
    sections: list = field(default_factory=list)
    definitions: list = field(default_factory=list)

    @property
    def bases_used(self):
        used = {m.basis for s in self.sections if s.kind == "metrics" for m in s.metrics}
        return [(k, *BASIS[k]) for k in BASIS if k in used]

    def add(self, section):
        if section is not None:
            self.sections.append(section)
        return section
