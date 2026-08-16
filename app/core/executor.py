"""Run a reconciliation result: perform what's automatable, report the rest.

The :class:`ActionRunner` is the bridge between the reconciler's plan and the
capture seams. For each action it either performs it (if a captured executor
exists and the action is marked ``AUTO_READY``) or files it under "needs
operator" — so today, with no writes captured, every action is reported and
nothing is performed, exactly as intended.

When you capture a write, mark its :class:`~app.domain.reconcile.ActionKind` as
a capability (so the reconciler emits it as ``AUTO_READY``) and verify its
:class:`~app.game.writes.WriteSpec`; the same actions then flow through
:meth:`ActionRunner.run` and get performed. No change to this class.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.reconcile import Action, Automation, ReconcileResult
from ..game.writes import CaptureRequired, EstateWriter


@dataclass
class ExecutionReport:
    """Outcome of running a plan."""

    performed: list[Action] = field(default_factory=list)
    reported: list[Action] = field(default_factory=list)  # needs operator (pending/manual)
    failed: list[tuple[Action, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{len(self.performed)} performed",
            f"{len(self.reported)} need you",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


class ActionRunner:
    """Executes the automatable actions of a plan; reports the rest."""

    def __init__(self, writer: EstateWriter) -> None:
        self._writer = writer

    async def run(self, result: ReconcileResult) -> ExecutionReport:
        report = ExecutionReport()
        for action in result.actions:
            if action.automation is not Automation.AUTO_READY:
                report.reported.append(action)
                continue
            try:
                await self._writer.perform(action)
                report.performed.append(action)
            except CaptureRequired:
                # Marked ready but no executor yet — treat as needs-operator
                # rather than an error, so a mislabelled capability degrades safely.
                report.reported.append(action)
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the loop
                report.failed.append((action, str(exc)))
        return report
