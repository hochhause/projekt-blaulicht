"""Turn a mission's requirement *text* into concrete vehicle-type demands.

.. note::
   This regex engine is the **secondary** requirement path. Base requirements
   now come from structured data via :class:`app.domain.resolver.
   MissionRequirementResolver` (``einsaetze.json`` class→count joined with
   ``Vehicle.mscv``) — no parsing needed. Use this engine only for per-instance
   detail the static catalogue lacks: exact crew/water/pump scaling and
   patient/prisoner cues read off the live mission page.

The engine takes the list of requirement lines a mission advertises (one string
per requirement, as read from the mission page) and, for each line, works out:

* **which vehicle types can satisfy it** — every type whose base pattern names
  it or whose extend rule scales it; and
* **how many are needed** — 1 for a plain match, or the scaled count from the
  strictest extend rule (crew/water/pump capacity, or an enumerated range).

It returns one :class:`Requirement` per recognised line. The requirement holds
a *set* of acceptable type IDs rather than a single type, because a line like
"LF" is satisfied by any of several fire-engine types; picking the concrete,
nearest-available vehicle is the matcher's job at dispatch time.

Lines that match nothing are collected in :attr:`Resolution.unmatched` — never
silently dropped. Those reports are exactly how the German patterns get
extended over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .assets import AssetLibrary


@dataclass(frozen=True)
class Requirement:
    """One satisfiable requirement line and the vehicles that can fill it."""

    source: str
    """The original requirement line, kept for logging and debugging."""

    type_ids: frozenset[int]
    """Vehicle type IDs that satisfy this requirement (any one of them)."""

    count: int
    """How many vehicles the line demands."""

    matchless_type_ids: frozenset[int] = frozenset()
    """Subset of :attr:`type_ids` flagged ``is_matchless`` — these must be
    dispatched as their exact type, not substituted."""


@dataclass
class Resolution:
    """The full result of resolving a mission's requirements."""

    requirements: list[Requirement] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)

    @property
    def total_vehicles(self) -> int:
        """Sum of demanded counts across all requirements."""
        return sum(r.count for r in self.requirements)


class RequirementEngine:
    """Resolves requirement text against a loaded :class:`AssetLibrary`."""

    def __init__(self, library: AssetLibrary) -> None:
        self._library = library

    def resolve_line(self, line: str) -> Requirement | None:
        """Resolve a single requirement line, or ``None`` if nothing matches."""
        line = line.strip()
        if not line:
            return None

        candidates: list[tuple[int, int]] = []  # (type_id, needed_count)
        matchless: set[int] = set()
        for vtype in self._library.types.values():
            if not vtype.matches(line):
                continue
            candidates.append((vtype.id, vtype.required_count(line)))
            if vtype.is_matchless:
                matchless.add(vtype.id)

        if not candidates:
            return None

        type_ids = frozenset(tid for tid, _ in candidates)
        # A line read strictly needs enough vehicles for its largest demand
        # (e.g. an enumerated "3-4 police -> 2 cars" beats a bare base match).
        count = max(cnt for _, cnt in candidates)
        return Requirement(
            source=line,
            type_ids=type_ids,
            count=count,
            matchless_type_ids=frozenset(matchless),
        )

    def resolve(self, lines: list[str]) -> Resolution:
        """Resolve every requirement line of a mission."""
        resolution = Resolution()
        for line in lines:
            requirement = self.resolve_line(line)
            if requirement is None:
                stripped = line.strip()
                if stripped:
                    resolution.unmatched.append(stripped)
            else:
                resolution.requirements.append(requirement)
        return resolution
