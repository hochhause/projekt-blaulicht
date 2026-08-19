"""FastAPI panel — dashboard, account config, start/stop, live dry-run.

Built by :func:`create_app` from the store, orchestrator, secret box and auth so
it stays testable and free of module-level globals. Server-rendered (Jinja2 +
form posts); no SPA. All mutating routes require the admin session.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..core.account_config import DEFAULT_CONFIG, AccountConfig
from .auth import AdminAuth

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class NotAuthenticated(Exception):
    """Raised by the auth dependency; handled by redirecting to /login."""


def _opt_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def create_app(store, orchestrator, secret_box, settings, auth: AdminAuth, log_store=None) -> FastAPI:
    app = FastAPI(title="Projekt Blaulicht")

    def require_auth(request: Request) -> None:
        if not auth.valid(request.cookies.get(AdminAuth.COOKIE)):
            raise NotAuthenticated()

    @app.exception_handler(NotAuthenticated)
    async def _redirect_login(request: Request, exc: NotAuthenticated):
        return RedirectResponse("/login", status_code=303)

    # -- health (no auth) --------------------------------------------------
    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    # -- auth --------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return _TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login(request: Request, password: str = Form(...)):
        if not auth.check(password):
            return _TEMPLATES.TemplateResponse(
                request, "login.html", {"error": "Wrong password."}, status_code=401
            )
        token = auth.issue()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(AdminAuth.COOKIE, token, httponly=True, samesite="lax")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        auth.revoke(request.cookies.get(AdminAuth.COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(AdminAuth.COOKIE)
        return resp

    # -- dashboard ---------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(require_auth)):
        live = orchestrator.status()
        rows = []
        for account in store.list_accounts():
            state = store.get_state(account.id) or {}
            rows.append(
                {
                    "account": account,
                    "status": live.get(account.id, state.get("status", "stopped")),
                    "totals": store.totals(account.id),
                    "last_cycle": state.get("last_cycle"),
                    "last_error": state.get("last_error"),
                }
            )
        return _TEMPLATES.TemplateResponse(request, "dashboard.html", {"rows": rows})

    # -- per-account details / log ----------------------------------------
    @app.get("/accounts/{account_id}/details", response_class=HTMLResponse)
    async def details(account_id: int, request: Request, _=Depends(require_auth)):
        account = store.get_account(account_id)
        if account is None:
            return RedirectResponse("/", status_code=303)
        state = store.get_state(account_id) or {}
        log = log_store.recent(account_id) if log_store is not None else []
        cfg = AccountConfig.from_dict(_load_config(store, account_id))
        return _TEMPLATES.TemplateResponse(
            request,
            "details.html",
            {
                "account": account,
                "status": orchestrator.status().get(account_id, state.get("status", "stopped")),
                "totals": store.totals(account_id),
                "last_cycle": state.get("last_cycle"),
                "last_error": state.get("last_error"),
                "log": log,
                "cfg": cfg,
            },
        )

    # -- global settings ---------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, _=Depends(require_auth)):
        return _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "message": None,
                "error": None,
                "running": orchestrator.running,
                "data_dir": str(settings.data_dir),
                "secret_set": bool(settings.secret_key),
            },
        )

    @app.post("/settings/password")
    async def change_password(
        request: Request,
        _=Depends(require_auth),
        current: str = Form(...),
        new_password: str = Form(...),
    ):
        ok = auth.change_password(current, new_password)
        return _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "message": "Password changed." if ok else None,
                "error": None if ok else "Current password is wrong (or new one empty).",
                "running": orchestrator.running,
                "data_dir": str(settings.data_dir),
                "secret_set": bool(settings.secret_key),
            },
            status_code=200 if ok else 400,
        )

    # -- account create / edit --------------------------------------------
    @app.post("/accounts")
    async def create_account(
        _=Depends(require_auth),
        label: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
    ):
        account_id = store.add_account(label, email)
        store.set_secret(account_id, secret_box.seal(password))
        store.set_config(account_id, json.dumps(DEFAULT_CONFIG.to_dict()))
        store.set_state(account_id, "stopped")
        return RedirectResponse("/", status_code=303)

    @app.get("/accounts/{account_id}", response_class=HTMLResponse)
    async def edit_account(account_id: int, request: Request, _=Depends(require_auth)):
        account = store.get_account(account_id)
        if account is None:
            return RedirectResponse("/", status_code=303)
        config = AccountConfig.from_dict(_load_config(store, account_id))
        return _TEMPLATES.TemplateResponse(
            request, "account.html", {"account": account, "cfg": config}
        )

    @app.post("/accounts/{account_id}")
    async def update_account(
        account_id: int,
        _=Depends(require_auth),
        label: str = Form(...),
        email: str = Form(...),
        password: str = Form(""),
        radius_km: float = Form(40.0),
        poll_seconds: float = Form(45.0),
        jitter_seconds: float = Form(10.0),
        own_credit_min: int = Form(0),
        own_credit_max: int = Form(0),
        alliance_credit_min: int = Form(0),
        alliance_credit_max: int = Form(0),
        alliance_min_age_minutes: float = Form(0.0),
        alliance_max_age_minutes: float = Form(0.0),
        max_vehicles_per_mission: str = Form(""),
        order: str = Form("nearest"),
        mission_source: str = Form("both"),
        batch_size: str = Form(""),
        allowed_categories: str = Form(""),
        alliance_radius_km: str = Form(""),
        transport_enabled: str = Form(""),
        transport_max_distance_km: float = Form(100.0),
        transport_max_tax: str = Form(""),
        transport_prefer_own: str = Form(""),
        transport_allow_alliance: str = Form(""),
    ):
        store.update_account(account_id, label=label, email=email)
        if password.strip():  # only replace when a new password is given
            store.set_secret(account_id, secret_box.seal(password))
        config = AccountConfig(
            radius_km=radius_km,
            poll_seconds=poll_seconds,
            jitter_seconds=jitter_seconds,
            own_credit_min=own_credit_min,
            own_credit_max=own_credit_max,
            alliance_credit_min=alliance_credit_min,
            alliance_credit_max=alliance_credit_max,
            alliance_min_age_minutes=alliance_min_age_minutes,
            alliance_max_age_minutes=alliance_max_age_minutes,
            max_vehicles_per_mission=_opt_int(max_vehicles_per_mission),
            order=order,
            mission_source=mission_source,
            batch_size=_opt_int(batch_size),
            allowed_categories=tuple(
                c.strip() for c in allowed_categories.split(",") if c.strip()
            ),
            alliance_radius_km=(float(alliance_radius_km) if alliance_radius_km.strip() else None),
            transport_enabled=bool(transport_enabled),
            transport_max_distance_km=transport_max_distance_km,
            transport_max_tax=_opt_int(transport_max_tax),
            transport_prefer_own=bool(transport_prefer_own),
            transport_allow_alliance=bool(transport_allow_alliance),
        )
        store.set_config(account_id, json.dumps(config.to_dict()))
        return RedirectResponse("/", status_code=303)

    # -- lifecycle actions -------------------------------------------------
    @app.post("/accounts/{account_id}/enable")
    async def enable(account_id: int, _=Depends(require_auth)):
        store.set_enabled(account_id, True)
        await orchestrator.start(account_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/disable")
    async def disable(account_id: int, _=Depends(require_auth)):
        store.set_enabled(account_id, False)
        await orchestrator.stop(account_id)
        store.set_state(account_id, "stopped")
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/delete")
    async def delete(account_id: int, _=Depends(require_auth)):
        await orchestrator.stop(account_id)
        store.delete_account(account_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/stop-all")
    async def stop_all(_=Depends(require_auth)):
        await orchestrator.stop_all()
        return RedirectResponse("/", status_code=303)

    return app


def _load_config(store, account_id: int) -> dict:
    raw = store.get_config(account_id)
    return json.loads(raw) if raw else {}
