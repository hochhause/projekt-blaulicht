"""Desired-state reconciliation — the estate manager's core.

Diffs the actual estate (:class:`~app.domain.estate.BuildingState`) against the
declared :class:`~app.domain.layout.EstateLayout` and emits the ordered list of
:class:`Action` s that would close the gap: level-ups, extensions, vehicle
purchases, hires, and staffing reviews.

Every action carries an :class:`Automation` classification:

* ``AUTO_READY`` — a captured write path exists; the executor can perform it.
* ``AUTO_PENDING_CAPTURE`` — automatable in principle, but the game has no write
  API and this action's request shape hasn't been captured yet, so for now it is
  **reported** for the operator to do by hand.
* ``MANUAL`` — needs human judgement (e.g. selling surplus vehicles) and is
  always reported, never automated.

Because no estate-write shapes are captured yet, actions are currently
``AUTO_PENDING_CAPTURE`` or ``MANUAL`` — i.e. the bot tells you exactly what to
do. As each write is captured, flip its action to ``AUTO_READY`` and wire the
executor; the reconciler and reporting need no change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import AbstractSet

from .assets import AssetLibrary
from .crew import building_crew_requirement
from .estate import BuildingState
from .layout import EstateLayout


class ActionKind(str, Enum):
    LEVEL_UP = "level_up"
    BUILD_EXTENSION = "build_extension"
    BUY_VEHICLE = "buy_vehicle"
    EXCESS_VEHICLE = "excess_vehicle"
    HIRE_PERSONNEL = "hire_personnel"
    START_TRAINING = "start_training"
    REVIEW_STAFFING = "review_staffing"


class Automation(str, Enum):
    AUTO_READY = "auto_ready"
    AUTO_PENDING_CAPTURE = "auto_pending_capture"
    MANUAL = "manual"


# How each action behaves *by default*, before any capture. Kinds that need
# human judgement stay MANUAL even once "captured"; the rest are automatable in
# principle and become AUTO_READY when their kind is in the capabilities set.
DEFAULT_AUTOMATION: dict[ActionKind, Automation] = {
    ActionKind.LEVEL_UP: Automation.AUTO_PENDING_CAPTURE,
    ActionKind.BUILD_EXTENSION: Automation.AUTO_PENDING_CAPTURE,
    ActionKind.BUY_VEHICLE: Automation.AUTO_PENDING_CAPTURE,
    ActionKind.HIRE_PERSONNEL: Automation.AUTO_PENDING_CAPTURE,
    ActionKind.START_TRAINING: Automation.AUTO_PENDING_CAPTURE,
    ActionKind.EXCESS_VEHICLE: Automation.MANUAL,
    ActionKind.REVIEW_STAFFING: Automation.MANUAL,
}


def _automation(kind: ActionKind, capabilities: AbstractSet[ActionKind]) -> Automation:
    """Resolve an action's automation given the captured *capabilities*.

    A kind that is captured becomes ``AUTO_READY`` — unless its default is
    ``MANUAL`` (human judgement), which capture never overrides.
    """
    default = DEFAULT_AUTOMATION.get(kind, Automation.AUTO_PENDING_CAPTURE)
    if default is Automation.MANUAL:
        return Automation.MANUAL
    return Automation.AUTO_READY if kind in capabilities else default


@dataclass(frozen=True)
class Action:
    """One concrete step toward the desired state."""

    kind: ActionKind
    automation: Automation
    building_id: int
    detail: str
    reason: str
    quantity: int = 1
    building_caption: str = ""

    def __str__(self) -> str:
        where = self.building_caption or f"building {self.building_id}"
        qty = f" x{self.quantity}" if self.quantity != 1 else ""
        return f"[{self.kind.value}{qty}] {where}: {self.detail}"


@dataclass
class ReconcileResult:
    actions: list[Action] = field(default_factory=list)

    def by_automation(self, automation: Automation) -> list[Action]:
        return [a for a in self.actions if a.automation is automation]

    @property
    def automatable(self) -> list[Action]:
        """Actions that can be performed now (a captured executor exists)."""
        return self.by_automation(Automation.AUTO_READY)

    @property
    def needs_operator(self) -> list[Action]:
        """Everything the operator must currently do by hand — pending-capture
        plus inherently-manual. This is the 'action required' list."""
        return [a for a in self.actions if a.automation is not Automation.AUTO_READY]

    def report(self) -> str:
        """A grouped, human-readable 'action required' summary."""
        if not self.actions:
            return "Estate matches layout — no action required."
        lines: list[str] = []
        groups = [
            (Automation.AUTO_READY, "Will be done automatically"),
            (Automation.AUTO_PENDING_CAPTURE, "Action required (automatable once captured)"),
            (Automation.MANUAL, "Action required (manual — needs your judgement)"),
        ]
        for automation, heading in groups:
            items = self.by_automation(automation)
            if not items:
                continue
            lines.append(f"\n{heading}:")
            lines.extend(f"  - {a}" for a in items)
        return "\n".join(lines).lstrip("\n")


def reconcile(
    estate: list[BuildingState],
    layout: EstateLayout,
    library: AssetLibrary,
    capabilities: AbstractSet[ActionKind] = frozenset(),
) -> ReconcileResult:
    """Compute the actions needed to bring *estate* in line with *layout*.

    *capabilities* is the set of action kinds whose write has been captured;
    those come back as ``AUTO_READY`` (ready to perform) instead of reported.
    """
    result = ReconcileResult()

    for state in estate:
        plan = layout.for_building(state.id, state.building_type)
        if plan is None:
            continue  # unmanaged building — leave it alone

        _reconcile_level(result, state, plan, capabilities)
        _reconcile_extensions(result, state, plan, capabilities)
        _reconcile_vehicles(result, state, plan, capabilities)
        _reconcile_personnel(result, state, library, capabilities)

    return result


def _reconcile_level(result, state, plan, capabilities) -> None:
    if plan.target_level is not None and state.level < plan.target_level:
        result.actions.append(
            Action(
                kind=ActionKind.LEVEL_UP,
                automation=_automation(ActionKind.LEVEL_UP, capabilities),
                building_id=state.id,
                building_caption=state.caption,
                quantity=plan.target_level - state.level,
                detail=f"expand from level {state.level} to {plan.target_level}",
                reason="below target level",
            )
        )


def _reconcile_extensions(result, state, plan, capabilities) -> None:
    for ext_id in sorted(plan.extensions - state.extensions):
        result.actions.append(
            Action(
                kind=ActionKind.BUILD_EXTENSION,
                automation=_automation(ActionKind.BUILD_EXTENSION, capabilities),
                building_id=state.id,
                building_caption=state.caption,
                detail=f"build extension #{ext_id}",
                reason="extension missing",
            )
        )


def _reconcile_vehicles(result, state, plan, capabilities) -> None:
    for type_id, desired in sorted(plan.vehicles.items()):
        actual = state.vehicle_type_counts.get(type_id, 0)
        if actual < desired:
            result.actions.append(
                Action(
                    kind=ActionKind.BUY_VEHICLE,
                    automation=_automation(ActionKind.BUY_VEHICLE, capabilities),
                    building_id=state.id,
                    building_caption=state.caption,
                    quantity=desired - actual,
                    detail=f"buy vehicle type {type_id} ({actual}/{desired} present)",
                    reason="fewer than target",
                )
            )
        elif actual > desired:
            result.actions.append(
                Action(
                    kind=ActionKind.EXCESS_VEHICLE,
                    automation=_automation(ActionKind.EXCESS_VEHICLE, capabilities),
                    building_id=state.id,
                    building_caption=state.caption,
                    quantity=actual - desired,
                    detail=f"vehicle type {type_id} over target ({actual}/{desired}) — sell or move",
                    reason="more than target",
                )
            )


def _reconcile_personnel(result, state, library, capabilities) -> None:
    required = building_crew_requirement(library, state.vehicle_type_counts)
    if required > state.personnel_available:
        result.actions.append(
            Action(
                kind=ActionKind.HIRE_PERSONNEL,
                automation=_automation(ActionKind.HIRE_PERSONNEL, capabilities),
                building_id=state.id,
                building_caption=state.caption,
                quantity=required - state.personnel_available,
                detail=f"hire {required - state.personnel_available} "
                f"(need {required} crew, have {state.personnel_available})",
                reason="not enough personnel to staff stationed vehicles",
            )
        )
