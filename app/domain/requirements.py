"""Turn a mission's requirement *text* into concrete vehicle-type demands.

.. note::
   This regex engine is now a **tertiary, last-resort** fallback, retired from
   being primary. Base requirements come from
   :class:`app.domain.resolver.MissionRequirementResolver`, which prefers a
   revealed mission's ``missing_text`` (resolved against the LSSM category
   table, :mod:`app.domain.missingtext` — the authoritative remaining need,
   see ``docs/live-data-model.md``) and falls back to the structured catalogue
   (``einsaetze.json`` class→count joined with ``Vehicle.mscv``) for an
   untouched mission. This engine only still fires when BOTH of those return
   nothing at all for a mission (see ``app.domain.dispatch._derive_requirements``)
   — it keys requirements by raw vehicle CAPTION rather than class/category,
   which is why it can't simply be merged in (double-counting risk), and why
   it mis-parses quantity prefixes / lacks LF captions the newer resolvers
   don't have (TODO.md P1).

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

    delta_scope: str = "full"
    """Which already-assigned vehicles ``_apply_delta`` (dispatch.py) should
    subtract before dispatching more: ``"full"`` (default) subtracts both
    en-route (S3) and on-scene (S4) vehicles — the right rule for a raw,
    un-netted count (the catalogue fallback, and the casualty-driven layer,
    where neither an S3 nor an S4 vehicle has removed a patient/prisoner on
    its own). ``"en_route_only"`` subtracts S3 only — the right rule once a
    count has come from a mission's revealed ``missing_text``, which per
    docs/live-data-model.md is already "Am Einsatzort fehlend" (net of
    arrived/S4 vehicles): subtracting S4 again would double-count it."""


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
