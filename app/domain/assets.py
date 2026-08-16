"""Load the salvaged German vehicle assets into typed, queryable structures.

Two kinds of file live under ``data/de/vehicles/`` (recovered from the old
bot's git history):

``Vehicle.mscv``
    A **class map**: vehicle-class name → list of numeric type IDs, e.g.
    ``{"firetrucks": [0, 1, 6, ...], "ambulances": [28, 74], ...}``. Auxiliary
    grouping used by config and the matcher; the requirement engine itself
    works from per-type patterns.

``<id>.mscv`` (185 files)
    One per vehicle **type**. Shape::

        {
          "object": 0,                 # the numeric type ID
          "is_matchless": false,       # true → must be dispatched exactly, no substitute
          "additional": {              # optional; present on 45 files
            "base": {"water": 2000, "pump": 2000, "personnel": 9}
          },
          "pattern": {
            "base":   ["Löschfahrzeug \\(LF\\)", ...],   # identifies the type in requirement text
            "extend": {                                   # optional; scales the needed count
              "(?'firetrucksNeededWater'\\d+) l\\. Wasser": 1,   # capacity-scaling rule
              "([34]) Polizisten": 2                              # enumerated rule (32.mscv)
            }
          }
        }

The two ``extend`` dialects are described on :class:`ExtendRule`.

.. note::
   The interpretation of ``extend`` here is **reconstructed from the data**,
   not from original source we have. It reproduces the three canonical
   examples (LF water/crew scaling, RTW, FuStW police scaling) and is covered
   by tests, but should be validated against live missions as the bot runs and
   refined where real requirement text disagrees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path

from . import jsonc, regexcompat

# Capacity dimensions that a ``(?'...Water'\d+)``-style named group can scale
# against. A rule's dimension is inferred by matching its group name's suffix
# against these, then divided into the type's ``additional.base`` capacity.
CAPACITY_DIMENSIONS = ("personnel", "water", "pump")


@dataclass(frozen=True)
class ExtendRule:
    """One requirement-scaling rule from a type's ``pattern.extend`` map.

    There are two dialects, distinguished by :attr:`dimension`:

    * **Capacity-scaling** (``dimension`` set) — the pattern has a named group
      capturing a quantity, e.g. ``(?'firetrucksNeededWater'\\d+) l. Wasser``.
      The captured number is divided by this type's per-vehicle capacity for
      that dimension → number of vehicles needed (rounded up, minimum 1).

    * **Enumerated** (``dimension`` is ``None``) — the JSON value *is* the
      vehicle count for any match, e.g. ``"([34]) Polizisten": 2`` means "3 or
      4 police → 2 vehicles". The captured digits only select the range.
    """

    pattern: re.Pattern[str]
    value: int
    group_name: str | None = None
    dimension: str | None = None

    def needed(self, match: re.Match[str], capacities: dict[str, float]) -> int:
        """Vehicles this rule demands, given a successful *match*."""
        if self.dimension and self.group_name:
            cap = capacities.get(self.dimension, 0)
            if cap > 0:
                try:
                    quantity = int(match.group(self.group_name))
                except (IndexError, ValueError):
                    return self.value
                return max(1, ceil(quantity / cap))
        return self.value


@dataclass(frozen=True)
class VehicleType:
    """A dispatchable vehicle type: its requirement patterns, plus the crew and
    trailer facts used for personnel/estate management."""

    id: int
    is_matchless: bool
    base: tuple[re.Pattern[str], ...]
    extend: tuple[ExtendRule, ...]
    capacities: dict[str, float] = field(default_factory=dict)
    is_trailer: bool = False
    """True if this type IS a trailer (Anhänger) — passive, pulled by a carrier."""
    requires_trailer: bool = False
    """True if this type only works with a trailer attached."""

    @property
    def personnel(self) -> int:
        """Crew this vehicle carries when fully staffed (0 if unknown)."""
        return int(self.capacities.get("personnel", 0))

    @property
    def trailer_capacity(self) -> int:
        """How many trailers/modules this type can carry (0 if none)."""
        return int(self.capacities.get("trailer", 0))

    def matches(self, line: str) -> bool:
        """True if *line* names this type (any base pattern) or scales it
        (any extend rule)."""
        if any(p.search(line) for p in self.base):
            return True
        return any(r.pattern.search(line) for r in self.extend)

    def required_count(self, line: str) -> int:
        """How many of this type *line* demands. 1 if only a base pattern
        matched; otherwise the strictest (largest) extend demand."""
        counts = [
            r.needed(m, self.capacities)
            for r in self.extend
            if (m := r.pattern.search(line))
        ]
        return max(counts) if counts else 1


@dataclass(frozen=True)
class VehicleClassMap:
    """The ``Vehicle.mscv`` class → type-ID grouping, with a reverse index."""

    by_class: dict[str, tuple[int, ...]]
    class_of: dict[int, tuple[str, ...]]

    @classmethod
    def from_raw(cls, raw: dict[str, list[int]]) -> "VehicleClassMap":
        by_class = {name: tuple(ids) for name, ids in raw.items()}
        class_of: dict[int, list[str]] = {}
        for name, ids in by_class.items():
            for tid in ids:
                class_of.setdefault(tid, []).append(name)
        return cls(by_class, {tid: tuple(names) for tid, names in class_of.items()})


@dataclass(frozen=True)
class AssetLibrary:
    """Everything needed to turn requirement text into vehicle-type demands."""

    types: dict[int, VehicleType]
    class_map: VehicleClassMap

    @classmethod
    def load(cls, vehicles_dir: str | Path) -> "AssetLibrary":
        """Load ``Vehicle.mscv`` + every ``<id>.mscv`` from *vehicles_dir*."""
        directory = Path(vehicles_dir)
        if not directory.is_dir():
            raise FileNotFoundError(f"vehicle asset directory not found: {directory}")

        class_map = VehicleClassMap.from_raw(jsonc.load_file(directory / "Vehicle.mscv"))

        types: dict[int, VehicleType] = {}
        for path in directory.glob("*.mscv"):
            if path.name == "Vehicle.mscv":
                continue
            vtype = _parse_type(jsonc.load_file(path))
            types[vtype.id] = vtype

        return cls(types, class_map)


def _infer_dimension(group_name: str) -> str | None:
    """Map an extend group name (e.g. ``firetrucksNeededWater``) to a capacity
    dimension, or ``None`` for enumerated rules."""
    lowered = group_name.lower()
    for dim in CAPACITY_DIMENSIONS:
        if lowered.endswith(dim):
            return dim
    return None


# Extract the first named group's name from a translated Python pattern.
_NAMED_GROUP = re.compile(r"\(\?P<([A-Za-z_]\w*)>")


def _parse_extend(raw_extend: dict[str, int]) -> tuple[ExtendRule, ...]:
    rules: list[ExtendRule] = []
    for raw_pattern, value in raw_extend.items():
        translated = regexcompat.to_python(raw_pattern)
        compiled = re.compile(translated, re.IGNORECASE)
        name_match = _NAMED_GROUP.search(translated)
        group_name = name_match.group(1) if name_match else None
        dimension = _infer_dimension(group_name) if group_name else None
        rules.append(
            ExtendRule(
                pattern=compiled,
                value=int(value),
                group_name=group_name,
                dimension=dimension,
            )
        )
    return tuple(rules)


def _parse_type(raw: dict) -> VehicleType:
    pattern = raw.get("pattern", {}) or {}
    base = tuple(regexcompat.compile(p) for p in pattern.get("base", []) or [])
    extend = _parse_extend(pattern.get("extend", {}) or {})

    # Numeric capacities live under additional.base; ignore boolean flags such
    # as is_trailer / is_unique, which aren't scaling dimensions.
    additional_base = (raw.get("additional", {}) or {}).get("base", {}) or {}
    capacities = {
        dim: float(val)
        for dim, val in additional_base.items()
        if isinstance(val, (int, float)) and not isinstance(val, bool)
    }

    return VehicleType(
        id=int(raw["object"]),
        is_matchless=bool(raw.get("is_matchless", False)),
        base=base,
        extend=extend,
        capacities=capacities,
        is_trailer=bool(additional_base.get("is_trailer", False)),
        requires_trailer=bool(additional_base.get("is_trailer_required", False)),
    )
