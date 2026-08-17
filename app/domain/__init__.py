"""Pure game-logic layer — no network, no framework, fully unit-testable.

Convenience re-exports so callers can do ``from app.domain import
AssetLibrary, RequirementEngine`` without knowing the module split.
"""

from .assets import AssetLibrary, ExtendRule, VehicleClassMap, VehicleType
from .catalog import MissionCatalog, MissionType
from .coverage import Assignment, CoverageResult, select_coverage
from .crew import CrewGap, building_crew_requirement, crew_gap, required_crew
from .dispatch import DispatchOrder, DispatchPlan, DispatchPolicy, MissionPlan, plan_dispatches
from .estate import BuildingState
from .geo import haversine_km, nearest_km, within_radius
from .layout import BuildingLayout, EstateLayout
from .reconcile import Action, ActionKind, Automation, ReconcileResult, reconcile
from .requirements import Requirement, RequirementEngine, Resolution
from .resolver import MissionRequirementResolver
from .training import TrainingCatalog, TrainingReport, analyze_fleet
from .transport import (
    TransportDecision,
    TransportKind,
    TransportPolicy,
    TransportRequest,
    TransportTarget,
    route_transport,
    route_transports,
)

__all__ = [
    # assets
    "AssetLibrary",
    "VehicleType",
    "VehicleClassMap",
    "ExtendRule",
    # requirements — primary (structured) path
    "MissionCatalog",
    "MissionType",
    "MissionRequirementResolver",
    "Requirement",
    "Resolution",
    # requirements — secondary (regex scaling) path
    "RequirementEngine",
    # coverage (multi-role vehicle matching)
    "select_coverage",
    "CoverageResult",
    "Assignment",
    # dispatch planning
    "plan_dispatches",
    "DispatchPlan",
    "DispatchPolicy",
    "DispatchOrder",
    "MissionPlan",
    # transport routing
    "route_transport",
    "route_transports",
    "TransportRequest",
    "TransportTarget",
    "TransportPolicy",
    "TransportDecision",
    "TransportKind",
    # estate management
    "EstateLayout",
    "BuildingLayout",
    "BuildingState",
    "reconcile",
    "ReconcileResult",
    "Action",
    "ActionKind",
    "Automation",
    "required_crew",
    "building_crew_requirement",
    "crew_gap",
    "CrewGap",
    # training
    "TrainingCatalog",
    "TrainingReport",
    "analyze_fleet",
    # geo
    "haversine_km",
    "nearest_km",
    "within_radius",
]
