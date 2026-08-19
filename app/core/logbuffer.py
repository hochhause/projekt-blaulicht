"""In-memory per-account activity log for the panel's details view.

A small, thread-safe ring buffer per account — the orchestrator (event loop)
writes cycle summaries; the web layer (threadpool) reads them. Not persisted:
it's a live "what is it doing right now" view, capped so it can't grow without
bound. A global sequence stamps every entry so the dashboard can merge all
accounts' logs into one correctly-ordered compound feed.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LogEntry:
    ts: str       # display time HH:MM:SS
    message: str
    seq: int      # global monotonic order across all accounts


class LogStore:
    def __init__(self, capacity: int = 500) -> None:
        self._cap = capacity
        self._by_account: dict[int, deque[LogEntry]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, account_id: int, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._seq += 1
            buf = self._by_account.get(account_id)
            if buf is None:
                buf = self._by_account[account_id] = deque(maxlen=self._cap)
            buf.append(LogEntry(stamp, message, self._seq))

    def extend(self, account_id: int, messages: list[str]) -> None:
        for message in messages:
            self.append(account_id, message)

    def recent(self, account_id: int) -> list[LogEntry]:
        """Newest-first recent entries for one account."""
        with self._lock:
            return list(reversed(self._by_account.get(account_id, ())))

    def recent_all(self, limit: int = 200) -> list[tuple[int, LogEntry]]:
        """Newest-first (account_id, entry) across all accounts — the compound feed."""
        with self._lock:
            merged = [(aid, e) for aid, buf in self._by_account.items() for e in buf]
        merged.sort(key=lambda t: t[1].seq, reverse=True)
        return merged[:limit]

    def clear(self, account_id: int) -> None:
        with self._lock:
            self._by_account.pop(account_id, None)
