"""Projekt Blaulicht — a private, multi-account auto-dispatcher for
Leitstellenspiel.de (German server).

Package layout::

    app.game     HTTP session + typed client for the ten game endpoints
    app.domain   pure game logic: asset loading, requirement matching, geo
    app.core     (later) orchestrator, per-account runner, secret handling
    app.web      (later) FastAPI panel

The ``domain`` layer is deliberately free of any network or framework
dependency so it can be unit-tested in isolation.
"""

__version__ = "0.1.0"
