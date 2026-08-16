"""Geographic helpers — great-circle distance and radius filtering.

Dispatch decisions are gated on distance: a mission is only a candidate for an
account if it falls within a configured radius of one of that account's
buildings. The game exposes latitude/longitude on both buildings and missions,
so we compute distance directly rather than trusting any server-side "range"
field (which the bot must not depend on).
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088  # mean Earth radius (IUGG)

# A latitude/longitude pair in decimal degrees.
LatLon = tuple[float, float]


def haversine_km(a: LatLon, b: LatLon) -> float:
    """Great-circle distance between two ``(lat, lon)`` points, in kilometres."""
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def nearest_km(point: LatLon, origins: list[LatLon]) -> float | None:
    """Distance from *point* to the closest of *origins*, or ``None`` if empty.

    Used to test a mission against **all** of an account's buildings at once —
    the mission is in range if any building is within the radius.
    """
    if not origins:
        return None
    return min(haversine_km(point, o) for o in origins)


def within_radius(point: LatLon, origins: list[LatLon], radius_km: float) -> bool:
    """True if *point* lies within *radius_km* of any of *origins*."""
    d = nearest_km(point, origins)
    return d is not None and d <= radius_km
