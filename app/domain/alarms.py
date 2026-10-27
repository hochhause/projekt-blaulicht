"""Alarm tracker — how many real sends a mission takes to close.

Sasha, 2026-10-27: "keep track of how many missions we complete in X sends
because the goal would be to have majority of missions at 1 or 2 sends (one
initial send and if needed one reinforcement for expansion or RD)." A
parasite send or S5 (Sprechwunsch) processing doesn't count — only an
initial dispatch or a real reinforcement does (see
:attr:`~app.domain.dispatch.MissionPlan.counts_as_alarm`).

State only — the runner records an alarm per qualifying send and finalizes a
mission once it drops out of the live feed (the same "it's done" signal
``mission_interaction`` pruning already uses). A mission never sent a real
alarm (fully handled by someone else, or only ever a parasite/VGE-spare
claim) leaves no trace: the metric is about OUR dispatch efficiency on
missions we actually worked, not about missions we never engaged.

In-memory only (Sasha: "doesn't need to survive a restart") — a real account
restart makes a fresh :class:`~app.core.account_runner.AccountRunner` -> a
fresh tracker, same as :class:`~app.domain.strikes.StrikeTracker`.
"""

from __future__ import annotations


class AlarmTracker:
    def __init__(self) -> None:
        self._counts: dict[int, int] = {}  # mission_id -> real alarms so far

    def record(self, mission_id: int) -> None:
        """Count one real alarm (initial send or reinforcement) to *mission_id*."""
        self._counts[mission_id] = self._counts.get(mission_id, 0) + 1

    def finalize(self, live_ids) -> dict[int, int]:
        """Drop tracking for any mission no longer in *live_ids*, returning
        ``{alarm_count: how many such missions just closed}`` so the caller
        can fold the delta into a persisted distribution. Empty most cycles
        (nothing closed). A mission still in *live_ids* keeps accumulating."""
        gone = [mid for mid in self._counts if mid not in live_ids]
        closed: dict[int, int] = {}
        for mid in gone:
            count = self._counts.pop(mid)
            closed[count] = closed.get(count, 0) + 1
        return closed
