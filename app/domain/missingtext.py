"""Resolve a live mission's ``missing_text`` into vehicle-type demands.

Per ``docs/live-data-model.md`` / ``docs/lssm-requirement-mapping.md``, once a
mission has at least one unit on scene the game reveals ``missing_text`` — a
JSON blob of three groups (``vehicles``, ``personnel``, ``other``) whose
entries are requirement **CATEGORY** names with counts (e.g.
``"4 Löschfahrzeuge (LF)"``, not a vehicle caption like ``"LF 20"``). This is
the **authoritative remaining need** ("Am Einsatzort fehlend"), replacing the
old regex engine's guesswork for missions that have revealed it.

The category → vehicle-type-id table is LSSM V4's ``vehiclesByRequirement`` +
``staff`` data (``data/reference/lssm-missing-requirements.json``):

* ``equipment``-gated categories (GW/AB modules) are matched **conservatively**
  — we don't track per-vehicle equipment state yet, so any carrier of the base
  type counts; a dispatch that turns out to lack the module simply strikes
  out (see ``app/domain/strikes.py``), per the documented fallback.
* ``conditionalVehicles`` (extra types allowed only under a game SETTING, e.g.
  ``allow_rw_instead_of_lf``) are **never** added — off by default, since we
  don't detect game settings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .requirements import Requirement, Resolution

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PATH = _REPO_ROOT / "data" / "reference" / "lssm-missing-requirements.json"

# A chunk starts with "<count> " — split only there, so a comma INSIDE a
# category's own text (e.g. "Schlauchwagen (GW-L2 Wasser, SW 1000, ... )")
# is never mistaken for a boundary between two requirement categories.
_SPLIT_RE = re.compile(r",\s*(?=\d+\s)")
_COUNT_RE = re.compile(r"^(\d+)\s+(.+)$")


@dataclass(frozen=True)
class MissingTextGroups:
    """The three requirement groups a revealed ``missing_text`` blob carries.

    ``revealed`` is the field that matters for priority: it's True the moment
    the game hands back a parseable blob at all, even if ``vehicles`` and
    ``personnel`` are BOTH empty — which means "nothing more needed", the
    authoritative answer, not "unknown, guess from the catalogue". Conflating
    "empty because untouched" with "empty because satisfied" would re-derive
    a stale catalogue requirement for a mission that's already fully crewed
    (see docs/live-data-model.md).
    """

    vehicles: str = ""
    personnel: str = ""
    other: str = ""
    revealed: bool = False

    @property
    def has_vehicle_demand(self) -> bool:
        """True if ``vehicles`` or ``personnel`` has anything to resolve."""
        return bool(self.vehicles or self.personnel)


def parse_missing_text(raw: str) -> MissingTextGroups:
    """Parse a mission's raw ``missing_text`` field into its three groups.

    Empty/absent (untouched mission) or a malformed blob both yield an empty,
    **unrevealed** :class:`MissingTextGroups` — never raises, so a mission we
    can't parse is just treated as having nothing revealed yet (catalogue
    fallback applies). A well-formed JSON object is ``revealed=True`` only if
    it actually carries at least one of the ``vehicles``/``personnel``/
    ``other`` KEYS (even if empty — see the class docstring) — NOT merely
    "parsed as an object".

    Live-verified 2026-08-31: some UNTOUCHED (still red, ``vehicle_state=0``)
    prisoner-related missions reveal ``{"long_text": "Gefangene sollen
    abtransportiert werden."}`` — a flavor-text hint with none of the three
    expected keys at all. Treating that as "revealed, nothing more needed"
    (the old behavior: any parseable object → revealed) would have wrongly
    skipped the catalogue estimate for a mission that clearly still needs
    vehicles. Requiring an actual expected key closes this without breaking
    the confirmed "revealed but every group empty" case (real missions DO
    send back e.g. ``{"vehicles":"","personnel":"","other":""}`` once fully
    crewed — those keys are present, just empty).
    """
    text = (raw or "").strip()
    if not text or text[0] != "{":
        return MissingTextGroups()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return MissingTextGroups()
    if not isinstance(data, dict):
        return MissingTextGroups()
    revealed = any(key in data for key in ("vehicles", "personnel", "other"))
    return MissingTextGroups(
        vehicles=_normalize_spaces(data.get("vehicles")),
        personnel=_normalize_spaces(data.get("personnel")),
        other=_normalize_spaces(data.get("other")),
        revealed=revealed,
    )


def _normalize_spaces(value: Any) -> str:
    """Collapse ANY Unicode whitespace (the game uses U+00A0 non-breaking
    space both between a count and its category, e.g. "1\xa0Löschfahrzeug",
    and inside abbreviations like "z.\xa0B." — and it's already shown to be
    inconsistent about which invisible space character it uses where) to a
    plain space, so category text matches the reference table's plain-space
    keys. Uses the SAME \\s class _COUNT_RE/_SPLIT_RE already tolerate when
    splitting, so a stray thin/narrow-no-break space etc. can't reintroduce
    the same failure mode for a codepoint we haven't seen yet. Live-verified
    2026-09-11: without this, every multi-word category silently resolved to
    zero vehicles (a well-formed but unmatched lookup), so a "revealed"
    mission with real, visible needs (e.g. "1 Löschfahrzeug (LF)") was
    treated as needing nothing and skipped."""
    return re.sub(r"\s", " ", str(value or ""))


class MissingTextResolver:
    """Maps ``missing_text`` requirement-category strings to vehicle type IDs.

    Built from LSSM V4's ``vehiclesByRequirement`` + ``staff`` tables. Multiple
    display variants of the same category (singular/plural, "z. B."/"z.\xa0B.")
    map to the same type-id set, per the source data.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        by_text: dict[str, frozenset[int]] = {}
        for entry in data.get("vehiclesByRequirement", ()):
            ids = frozenset(int(v) for v in entry.get("vehicles", ()))
            for text in entry.get("texts", ()):
                by_text[text] = ids
        for group in data.get("staff", ()):
            ids = frozenset(int(v) for v in group.get("vehicles", ()))
            for text in group.get("texts", ()):
                by_text[text] = ids
        self._by_text = by_text

    @classmethod
    def load(cls, path: str | Path) -> "MissingTextResolver":
        """Load the category table from *path* (the LSSM mapping JSON)."""
        directory = Path(path)
        if not directory.is_file():
            raise FileNotFoundError(f"LSSM requirement mapping not found: {directory}")
        return cls(json.loads(directory.read_text(encoding="utf-8")))

    def resolve_line(self, chunk: str) -> Requirement | None:
        """Resolve one ``"<count> <category>"`` chunk, or ``None`` if the
        category isn't in the table (an unmatched — never dropped silently)."""
        chunk = chunk.strip()
        if not chunk:
            return None
        m = _COUNT_RE.match(chunk)
        count, category = (int(m.group(1)), m.group(2).strip()) if m else (1, chunk)
        type_ids = self._by_text.get(category)
        if type_ids is None:
            return None
        return Requirement(source=category, type_ids=type_ids, count=count)

    def resolve(self, raw_group: str) -> Resolution:
        """Resolve a full requirement-group string (``vehicles`` or
        ``personnel``), e.g. ``"4 Löschfahrzeuge (LF), 1 ELW 1"``."""
        resolution = Resolution()
        text = (raw_group or "").strip()
        if not text:
            return resolution
        for chunk in _SPLIT_RE.split(text):
            requirement = self.resolve_line(chunk)
            if requirement is None:
                stripped = chunk.strip()
                if stripped:
                    resolution.unmatched.append(stripped)
            else:
                resolution.requirements.append(requirement)
        return resolution


@lru_cache(maxsize=1)
def default_resolver() -> MissingTextResolver:
    """The resolver built from the repo's committed LSSM mapping data.

    Cached: the table is static reference data, identical for every account
    and every mission, so there is no reason to reload/reparse it per call.
    """
    return MissingTextResolver.load(_DEFAULT_PATH)
