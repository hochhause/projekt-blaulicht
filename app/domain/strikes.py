"""Strike tracker — stop chasing missions that need a vehicle you never have.

When a mission can't be fully crewed because a vehicle class is unavailable,
that class gets a **strike**. After three strikes (within a 24 h window) the
class is "struck out" and missions requiring it are skipped, until the window
lapses or the bot restarts (a fresh tracker). This avoids the loop pointlessly
re-attempting missions it can't complete every cycle.

State only — the planner records strikes and queries struck-out classes; the
:class:`~app.core.account_runner.AccountRunner` owns one tracker per account, so
stopping/starting an account (a "restart") resets it.
"""

from __future__ import annotations

DEFAULT_THRESHOLD = 3
DAY_SECONDS = 24 * 60 * 60


class StrikeTracker:
    def __init__(
        self,
        threshold: int = DEFAULT_THRESHOLD,
        window_seconds: float = DAY_SECONDS,
        on_struck=None,
    ) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._strikes: dict[str, list[float]] = {}
        # Called once with the class name when it crosses into struck-out.
        self._on_struck = on_struck

    def record(self, classes, now: float) -> None:
        """Add a strike (at time *now*) for each shortfall class."""
        for cls in classes:
            was_struck = self.is_struck(cls, now)
            self._strikes.setdefault(cls, []).append(now)
            if not was_struck and self._on_struck is not None and self.is_struck(cls, now):
                self._on_struck(cls)

    def is_struck(self, cls: str, now: float) -> bool:
        """True if *cls* has reached the threshold within the window. Prunes
        strikes older than the window as a side effect."""
        recent = [t for t in self._strikes.get(cls, ()) if now - t < self._window]
        if recent:
            self._strikes[cls] = recent
        elif cls in self._strikes:
            del self._strikes[cls]
        return len(recent) >= self._threshold

    def struck_classes(self, now: float) -> set[str]:
        return {cls for cls in list(self._strikes) if self.is_struck(cls, now)}
