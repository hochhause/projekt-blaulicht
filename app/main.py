"""Application entrypoint — wires store, secrets, orchestrator and web panel.

Run with::

    pip install -e ".[web]"
    BOT_SECRET_KEY=... ADMIN_PASSWORD=... uvicorn app.main:app --host 127.0.0.1 --port 8080

The whole thing is one asyncio process: the FastAPI panel plus the orchestrator
that supervises one runner per enabled account. On startup, enabled accounts are
resumed; on shutdown, all runners are stopped cleanly.
"""

from __future__ import annotations

import logging
import sys

# Load a local .env (if python-dotenv is installed) so BOT_SECRET_KEY,
# ADMIN_PASSWORD etc. persist across restarts — the secret key MUST be stable
# or previously-sealed credentials can't be decrypted. Must run before
# get_settings() reads the environment.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from .config import get_settings
from .core.logbuffer import LogStore
from .core.orchestrator import Orchestrator, default_worker_factory, make_cleanup
from .core.secrets import SecretBox
from .store import Store
from .web.app import create_app
from .web.auth import AdminAuth

logger = logging.getLogger(__name__)


def build_app():
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    if not settings.secret_key:
        sys.exit(
            "BOT_SECRET_KEY is required (it seals stored credentials). "
            "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)

    auth = AdminAuth(store)
    auth.bootstrap(settings.admin_password)
    if not auth.is_configured():
        logger.warning("no admin password set — set ADMIN_PASSWORD on first run")

    secret_box = SecretBox(settings.secret_key)
    log_store = LogStore()
    orchestrator = Orchestrator(default_worker_factory(store, secret_box, settings, log_store))
    cleanup = make_cleanup(store, secret_box, settings, log_store)
    app = create_app(store, orchestrator, secret_box, settings, auth, log_store, cleanup=cleanup)

    @app.on_event("startup")
    async def _resume_enabled() -> None:
        for account in store.list_accounts():
            if not account.enabled:
                continue
            try:
                await orchestrator.start(account.id)
            except Exception as exc:  # noqa: BLE001 — one bad account must not stop startup
                logger.exception("could not resume account %s", account.id)
                store.set_state(account.id, "error", error=str(exc))
        logger.info("orchestrator resumed %d account(s)", len(orchestrator.running))

    @app.on_event("shutdown")
    async def _stop_all() -> None:
        await orchestrator.stop_all()

    return app


app = build_app()
