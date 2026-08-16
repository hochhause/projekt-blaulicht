"""Estate write actions — the capture seams.

The game has **no write API**, so every management action (level up, build
extension, buy vehicle, hire, train, assign crew/trailer) must be a
browser-mimicking form POST, exactly like dispatch. Each one's real request
shape has to be **captured from a live account's devtools** before it can be
performed. None are captured yet.

This module makes that seam explicit and low-friction:

* :class:`WriteSpec` documents the best-guess request for each action — a
  checklist to confirm during capture. Routes/fields marked ``verified=False``
  are **guesses** (Rails conventions + known LSS patterns), not confirmed.
* :class:`EstateWriter` exposes one entry point, :meth:`perform`. Until an
  action's spec is verified and implemented, it raises :class:`CaptureRequired`
  — which the executor treats as "report, don't perform". Wiring a captured
  action is then: fill in the real :class:`WriteSpec`, set ``verified=True``,
  and implement its branch. Nothing else changes.

The one fully-captured write, dispatch, lives in :meth:`GameClient.dispatch`
and is the reference for what a verified write looks like.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.reconcile import Action, ActionKind
from .session import GameSession


class CaptureRequired(NotImplementedError):
    """Raised when a write is invoked before its request shape is captured."""

    def __init__(self, spec: "WriteSpec") -> None:
        super().__init__(
            f"'{spec.action}' needs a devtools capture before it can run. "
            f"Best guess: {spec.method} {spec.url_template}. {spec.notes}"
        )
        self.spec = spec


@dataclass(frozen=True)
class WriteSpec:
    """Best-guess request shape for one write, pending live capture.

    Fill these in from a real devtools capture, then set ``verified=True`` and
    implement the matching branch in :meth:`EstateWriter._perform_verified`.
    """

    action: ActionKind
    method: str  # best-guess HTTP method
    url_template: str  # best-guess route; {building_id} / {vehicle_id} placeholders
    fields: dict[str, str] = field(default_factory=dict)  # field -> note
    verified: bool = False
    notes: str = "UNVERIFIED best guess — confirm URL, method and fields on capture."


# Best-guess specs. ALL unverified until a capture confirms them. The routes are
# plausible Rails endpoints, not facts — do not trust them until verified.
ESTATE_WRITE_SPECS: dict[ActionKind, WriteSpec] = {
    ActionKind.LEVEL_UP: WriteSpec(
        action=ActionKind.LEVEL_UP,
        method="POST",
        url_template="/buildings/{building_id}/extension",
        fields={"authenticity_token": "CSRF from the building page"},
        notes="UNVERIFIED. In LSS a 'level' is an extension slot; confirm whether "
        "level-up and build-extension are the same request.",
    ),
    ActionKind.BUILD_EXTENSION: WriteSpec(
        action=ActionKind.BUILD_EXTENSION,
        method="POST",
        url_template="/buildings/{building_id}/extension",
        fields={"authenticity_token": "CSRF", "extension_id": "which extension"},
        notes="UNVERIFIED. Confirm the field carrying the extension id.",
    ),
    ActionKind.BUY_VEHICLE: WriteSpec(
        action=ActionKind.BUY_VEHICLE,
        method="POST",
        url_template="/buildings/{building_id}/vehicle",
        fields={"authenticity_token": "CSRF", "vehicle_type": "numeric type id"},
        notes="UNVERIFIED. Confirm route and the vehicle-type field name.",
    ),
    ActionKind.HIRE_PERSONNEL: WriteSpec(
        action=ActionKind.HIRE_PERSONNEL,
        method="POST",
        url_template="/buildings/{building_id}/hire",
        fields={"authenticity_token": "CSRF", "count": "how many to hire"},
        notes="UNVERIFIED. Confirm route and whether hiring is one-at-a-time.",
    ),
    ActionKind.START_TRAINING: WriteSpec(
        action=ActionKind.START_TRAINING,
        method="POST",
        url_template="/buildings/{building_id}/schooling",
        fields={
            "authenticity_token": "CSRF",
            "schooling_type": "course key",
            "count": "number of staff",
        },
        notes="UNVERIFIED. Confirm route, course-key field, and staff selection.",
    ),
}


class EstateWriter:
    """Performs estate writes for one account — once their shapes are captured."""

    def __init__(self, session: GameSession) -> None:
        self._s = session

    async def perform(self, action: Action) -> None:
        """Perform *action*, or raise :class:`CaptureRequired` if unsupported.

        Only actions whose :class:`WriteSpec` is ``verified`` are attempted; the
        rest raise, so the executor reports them instead.
        """
        spec = ESTATE_WRITE_SPECS.get(action.kind)
        if spec is None or not spec.verified:
            raise CaptureRequired(spec or _unknown_spec(action.kind))
        await self._perform_verified(action, spec)

    async def _perform_verified(self, action: Action, spec: WriteSpec) -> None:
        """Dispatch to the concrete, captured implementation.

        Add a branch here as each write is captured (see dispatch in
        GameClient for the pattern: GET the page for a fresh token, then POST
        the confirmed form). Until then this is unreachable (nothing verified).
        """
        raise CaptureRequired(spec)  # no verified writes yet


def _unknown_spec(kind: ActionKind) -> WriteSpec:
    return WriteSpec(
        action=kind,
        method="?",
        url_template="?",
        notes=f"No write spec defined for {kind.value}; this action is report-only.",
    )
