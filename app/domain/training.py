"""Which training course (Lehrgang) each vehicle needs, and fleet gap analysis.

Source data (``data/de/training.json``) is scraped from the Leitstellenspiel
wiki and maps a vehicle's **type name** (as the game shows it, e.g.
``vehicle_type_caption`` "NEF", "WLF") to the course it requires. A vehicle not
in the map needs no training.

Matching is **normalised** — case, spaces and punctuation are ignored — so
"HLF 20" and "HLF20", or "GW-Gefahrgut" in either casing, resolve the same.
This sidesteps the fragile vehicle-name↔type-id bridge: the game's vehicle API
already returns the type name, so we match on that directly.

Whether staff are *already* trained isn't cleanly readable, so this reports the
training a fleet **requires** — the "show me what's missing" the operator asked
for. Cross-referencing ``/api/schoolings`` for completed courses is a later
refinement.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from . import jsonc


def _normalise(name: str) -> str:
    """Fold a vehicle name to a match key: lowercase alphanumerics only."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


class _HasType(Protocol):
    """Anything with a vehicle id and a type name — e.g. game ``Vehicle``."""

    id: int
    type_caption: str


@dataclass(frozen=True)
class TrainingCatalog:
    """Vehicle-name → required-course lookup (normalised keys)."""

    _by_norm: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TrainingCatalog":
        raw = jsonc.load_file(path)
        mapping = raw.get("vehicle_training", {}) if isinstance(raw, dict) else {}
        by_norm = {_normalise(name): course for name, course in mapping.items()}
        return cls(by_norm)

    def required_course(self, vehicle_type_name: str) -> str | None:
        """The course a vehicle of this type name needs, or ``None``."""
        if not vehicle_type_name:
            return None
        return self._by_norm.get(_normalise(vehicle_type_name))

    def requires_training(self, vehicle_type_name: str) -> bool:
        return self.required_course(vehicle_type_name) is not None


@dataclass
class TrainingReport:
    """Training a fleet requires, grouped by course."""

    # course -> vehicle ids that need it
    by_course: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    # vehicle type names present that we couldn't classify (for wiki-data upkeep)
    unclassified: set[str] = field(default_factory=set)

    @property
    def total_vehicles(self) -> int:
        return sum(len(v) for v in self.by_course.values())

    def render(self) -> str:
        if not self.by_course:
            return "No special training required for the current fleet."
        lines = ["Training required:"]
        for course in sorted(self.by_course):
            ids = self.by_course[course]
            lines.append(f"  - {course}: {len(ids)} vehicle(s)")
        return "\n".join(lines)


def analyze_fleet(vehicles: Iterable[_HasType], catalog: TrainingCatalog) -> TrainingReport:
    """Group a fleet by the training each vehicle requires.

    *vehicles* is any iterable of objects exposing ``id`` and ``type_caption``
    (the game :class:`Vehicle` qualifies); duck-typed so this stays free of the
    game/network layer.
    """
    report = TrainingReport()
    for vehicle in vehicles:
        name = getattr(vehicle, "type_caption", "") or ""
        course = catalog.required_course(name)
        if course is not None:
            report.by_course[course].append(vehicle.id)
    return report
