"""Per-account gameplay config — stored as JSON, mapped to typed policies.

The web panel edits an :class:`AccountConfig`; it is persisted as a JSON blob in
the store and turned into the domain policies (:class:`DispatchPolicy`,
:class:`TransportPolicy`) the runner consumes. Pure and unit-tested.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..domain.dispatch import DispatchOrder, DispatchPolicy, MissionSource
from ..domain.transport import TransportPolicy


@dataclass(frozen=True)
class AccountConfig:
    # dispatch
    radius_km: float = 40.0
    poll_seconds: float = 45.0
    jitter_seconds: float = 10.0
    # Max vehicles to load per cycle (0 = load ALL — needed on big accounts so
    # the fleet isn't truncated and vehicles wrongly look unavailable).
    max_vehicles: int = 0
    # Cash bounds, separate per source (0 = no bound).
    own_credit_min: int = 0
    own_credit_max: int = 0
    alliance_credit_min: int = 0
    alliance_credit_max: int = 0
    # Alliance age-since-shared window, in MINUTES (0 = no bound).
    alliance_min_age_minutes: float = 0.0
    alliance_max_age_minutes: float = 0.0
    max_vehicles_per_mission: int | None = None
    order: str = DispatchOrder.NEAREST.value
    batch_size: int | None = 20
    allowed_categories: tuple[str, ...] = ()
    mission_source: str = MissionSource.BOTH.value
    alliance_radius_km: float | None = None
    # transport
    transport_enabled: bool = True
    transport_max_distance_km: float = 100.0
    transport_max_tax: int | None = None
    transport_prefer_own: bool = True
    transport_allow_alliance: bool = True

    def dispatch_policy(self) -> DispatchPolicy:
        try:
            order = DispatchOrder(self.order)
        except ValueError:
            order = DispatchOrder.NEAREST
        try:
            source = MissionSource(self.mission_source)
        except ValueError:
            source = MissionSource.BOTH
        return DispatchPolicy(
            radius_km=self.radius_km,
            own_credit_min=self.own_credit_min,
            own_credit_max=self.own_credit_max,
            alliance_credit_min=self.alliance_credit_min,
            alliance_credit_max=self.alliance_credit_max,
            alliance_min_age_seconds=int(self.alliance_min_age_minutes * 60),
            alliance_max_age_seconds=int(self.alliance_max_age_minutes * 60),
            max_vehicles_per_mission=self.max_vehicles_per_mission,
            order=order,
            batch_size=self.batch_size,
            allowed_categories=frozenset(self.allowed_categories),
            mission_source=source,
            alliance_radius_km=self.alliance_radius_km,
        )

    def transport_policy(self) -> TransportPolicy:
        return TransportPolicy(
            max_distance_km=self.transport_max_distance_km,
            max_tax=self.transport_max_tax,
            prefer_own=self.transport_prefer_own,
            allow_alliance=self.transport_allow_alliance,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AccountConfig":
        """Build from a stored dict, ignoring unknown keys and keeping defaults
        for anything missing (so old configs survive new fields)."""
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        values = {k: v for k, v in d.items() if k in known}
        if "allowed_categories" in values:  # JSON stores a list; keep it a tuple
            values["allowed_categories"] = tuple(values["allowed_categories"])
        return cls(**values)


DEFAULT_CONFIG = AccountConfig()
