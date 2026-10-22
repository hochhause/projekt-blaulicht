"""FastAPI panel — dashboard, account config, start/stop, live dry-run.

Built by :func:`create_app` from the store, orchestrator, secret box and auth so
it stays testable and free of module-level globals. Server-rendered (Jinja2 +
form posts); no SPA. All mutating routes require the admin session.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..core.account_config import DEFAULT_CONFIG, AccountConfig, GroupConfig
from ..core.orchestrator import GROUP_KEY
from .auth import AdminAuth

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Distinct per-account colours for the compound dashboard log, cycled by order.
_ACCOUNT_COLORS = [
    "#4a90ff", "#37c26b", "#ff9e2c", "#c678dd",
    "#56b6c2", "#e06c75", "#e5c07b", "#98c379",
]


# Group mode prefixes every line with "[#<account id>] " — strip it before
# classifying so the same rules below (many of them startswith-based) apply
# equally to solo and group log lines (Sasha, 2026-09-11: group mode's lines
# were falling through uncoloured entirely because of this prefix).
_ACCOUNT_TAG = re.compile(r"^\[#\d+\]\s*")
# Same tag, but capturing the id — lets a group-mode line be attributed back
# to the specific real account it concerns (Sasha, 2026-09-12: "add options
# for viewing compound log or per-account log"), since every group line is
# otherwise stored under one shared pseudo-account bucket (GROUP_KEY).
_ACCOUNT_TAG_ID = re.compile(r"^\[#(\d+)\]")


def _tag_account_id(message: str) -> int | None:
    """The real account id a group-mode log line concerns, or None for a
    group-wide summary line ("group: 3 account(s)...", "group cycle done...")
    that isn't about any one member specifically."""
    m = _ACCOUNT_TAG_ID.match(message.strip())
    return int(m.group(1)) if m else None


def _account_log(log_store, account_id: int) -> list[dict]:
    """This account's own log entries PLUS any group-mode line that concerns
    it specifically (a "[#<id>]"-tagged line) — group mode logs everything
    under one shared GROUP_KEY bucket, so an account's activity while
    running inside a group would otherwise be invisible on its own details
    page (Sasha, 2026-09-12). Group-wide summary lines (no tag) are left out
    here — they belong to no single account; see them via the dashboard's
    compound feed instead. The tag itself is stripped from the merged-in
    text since it's redundant on an already-account-scoped page."""
    if log_store is None:
        return []
    rows = [(e.seq, e.ts, e.message) for e in log_store.recent(account_id)]
    if account_id != GROUP_KEY:
        for e in log_store.recent(GROUP_KEY):
            if _tag_account_id(e.message) == account_id:
                rows.append((e.seq, e.ts, _ACCOUNT_TAG.sub("", e.message)))
    rows.sort(key=lambda t: t[0], reverse=True)
    return [{"seq": seq, "ts": ts, "message": msg, "cls": classify_log(msg)} for seq, ts, msg in rows]


_BREAKDOWN_CATEGORIES = ("own", "verband", "event")


def _format_ago(seconds: float | None) -> str:
    """Human "time since" for the mission breakdown's last-interaction
    column (Sasha, 2026-09-22). ``None`` (never dispatched to, per the
    account's recorded history) reads as "never", not "0s ago"."""
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _grouped_mission_breakdown(store, account_id: int) -> dict[str, list[dict]]:
    """This account's latest-cycle mission breakdown (Sasha, 2026-09-22),
    grouped by category for the details page's collapsible sections — always
    all three keys (own/verband/event) so the template doesn't need to guess
    which sections exist, even when a group is empty this cycle. Each row
    also gets `last_interaction_ago` — how long since we last actually
    dispatched a vehicle to it, from the durable per-mission record (distinct
    from the breakdown itself, which is only a per-cycle snapshot)."""
    interactions = store.mission_interactions(account_id)
    now = time.time()
    grouped: dict[str, list[dict]] = {c: [] for c in _BREAKDOWN_CATEGORIES}
    for row in store.mission_breakdown(account_id):
        row = dict(row)
        ts = interactions.get(row["mission_id"])
        row["last_interaction_ago"] = _format_ago(now - ts if ts is not None else None)
        grouped.setdefault(row["category"], []).append(row)
    return grouped


def classify_log(message: str) -> str:
    """Map a log line to a colour class (see base.html .log-* rules)."""
    m = _ACCOUNT_TAG.sub("", message.strip())
    low = m.lower()
    if m.startswith("FAILED") or "struck out" in low or low.startswith("error"):
        return "log-err"
    if "not enough" in low or "needs struck-out" in low or "missing" in low or low.startswith("short "):
        return "log-warn"
    if m.startswith(("dispatched", "sent to help", "sent reinforcements")) or "en route" in low:
        return "log-ok"
    if low.startswith((
        "polled", "plan:", "connected", "connecting", "cycle done", "hilferuf",
        "transport", "recalled", "sharing", "batch failed", "group:",
    )):
        return "log-info"
    if low.startswith(("unresolved", "idle")):
        return "log-muted"
    return ""


class NotAuthenticated(Exception):
    """Raised by the auth dependency; handled by redirecting to /login."""


def _opt_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def create_app(store, orchestrator, secret_box, settings, auth: AdminAuth, log_store=None, cleanup=None) -> FastAPI:
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
        accounts = store.list_accounts()
        color_by_id = {a.id: _ACCOUNT_COLORS[i % len(_ACCOUNT_COLORS)] for i, a in enumerate(accounts)}
        rows = []
        for account in accounts:
            state = store.get_state(account.id) or {}
            rows.append(
                {
                    "account": account,
                    "color": color_by_id[account.id],
                    "status": live.get(account.id, state.get("status", "stopped")),
                    "totals": store.totals(account.id),
                    "mission_status": store.mission_status(account.id),
                    "last_cycle": state.get("last_cycle"),
                    "last_error": state.get("last_error"),
                }
            )
        # Compound feed: all accounts' activity, newest first, colour-prefixed.
        # Each row also carries `filter_id` — the REAL account it concerns,
        # so the log's account dropdown (see dashboard.html) can show just
        # one member's slice even though every group-mode line is stored
        # under one shared GROUP_KEY bucket (Sasha, 2026-09-12: "add options
        # for viewing compound log or per-account log"). A group-wide
        # summary line with no "[#<id>]" tag has no single owner and so
        # never matches a per-account filter — only "All" shows it.
        compound = []
        if log_store is not None:
            label_by_id = {a.id: a.label for a in accounts}
            for aid, entry in log_store.recent_all(120):
                filter_id = _tag_account_id(entry.message) if aid == GROUP_KEY else aid
                compound.append(
                    {
                        "seq": entry.seq,
                        "ts": entry.ts,
                        "label": label_by_id.get(aid) or ("Group" if aid == GROUP_KEY else f"#{aid}"),
                        "color": color_by_id.get(aid, "#8697ad"),
                        "cls": classify_log(entry.message),
                        "message": entry.message,
                        "filter_id": filter_id if filter_id is not None else "",
                    }
                )
        log_filters = [{"id": str(a.id), "label": a.label} for a in accounts]

        # LEFT panel: per-account "vehicles we struggle to field" (collapsible).
        strike_panels = []
        for account in accounts:
            rows_sf = store.shortfalls(account.id, limit=10)
            top = max((it["count"] for it in rows_sf), default=0) or 1
            strike_panels.append(
                {
                    "label": account.label,
                    "color": color_by_id[account.id],
                    "total": sum(it["count"] for it in rows_sf),
                    "rows": [
                        {"cls": it["vehicle_class"], "count": it["count"],
                         "pct": round(it["count"] * 100 / top)}
                        for it in rows_sf
                    ],
                }
            )

        # RIGHT panel: headline stats + a 24h dispatch chart.
        stats = _dashboard_stats(store, accounts, color_by_id)

        group = {
            "running": orchestrator.group_running,
            "members": orchestrator.group_members,
            "status": orchestrator.group_status(),
            # Combined totals across members while the group runs — sums the
            # SAME per-account `stat` rows the normal dashboard already shows
            # (group dispatches are recorded per-account, see
            # default_group_factory's `stat` closure), no new persistence.
            "totals": _combined_totals(store, orchestrator.group_members),
        }
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"rows": rows, "compound": compound, "log_filters": log_filters, "strike_panels": strike_panels,
             "stats": stats, "group": group},
        )

    # -- group (symbiotic) mode -------------------------------------------
    @app.get("/group", response_class=HTMLResponse)
    async def group_page(request: Request, _=Depends(require_auth)):
        raw = store.get_group_config()
        cfg = GroupConfig.from_dict(json.loads(raw) if raw else {})
        return _TEMPLATES.TemplateResponse(
            request, "group.html",
            {"cfg": cfg, "accounts": store.list_accounts(),
             "running": orchestrator.group_running, "members": orchestrator.group_members},
        )

    @app.post("/group")
    async def group_save(
        _=Depends(require_auth),
        action: str = Form("save"),
        radius_km: float = Form(40.0),
        credit_min: int = Form(0),
        credit_max: int = Form(0),
        order: str = Form("nearest"),
        batch_size: str = Form(""),
        allowed_categories: str = Form(""),
        poll_seconds: float = Form(45.0),
        jitter_seconds: float = Form(10.0),
        transport_enabled: str = Form(""),
        participation: str = Form(""),
        radius_basis: str = Form("any"),
        max_token_per_account: str = Form(""),
        partial_starts: str = Form(""),
        share_min_credits: int = Form(0),
        members: list[int] = Form(default=[]),
    ):
        cfg = GroupConfig(
            radius_km=radius_km,
            credit_min=credit_min,
            credit_max=credit_max,
            order=order,
            batch_size=_opt_int(batch_size),
            allowed_categories=tuple(c.strip() for c in allowed_categories.split(",") if c.strip()),
            poll_seconds=poll_seconds,
            jitter_seconds=jitter_seconds,
            transport_enabled=bool(transport_enabled),
            participation=bool(participation),
            radius_basis=radius_basis,
            max_token_per_account=_opt_int(max_token_per_account),
            partial_starts=bool(partial_starts),
            share_min_credits=share_min_credits,
            member_ids=tuple(members),
        )
        store.set_group_config(json.dumps(cfg.to_dict()))
        if action == "start" and members:
            await orchestrator.start_group(list(members))
            return RedirectResponse("/", status_code=303)
        return RedirectResponse("/group", status_code=303)

    @app.post("/group/stop")
    async def group_stop(_=Depends(require_auth)):
        await orchestrator.stop_group()
        return RedirectResponse("/", status_code=303)

    # -- per-account details / log ----------------------------------------
    @app.get("/accounts/{account_id}/details", response_class=HTMLResponse)
    async def details(account_id: int, request: Request, _=Depends(require_auth)):
        account = store.get_account(account_id)
        if account is None:
            return RedirectResponse("/", status_code=303)
        state = store.get_state(account_id) or {}
        log = _account_log(log_store, account_id)
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
                "mission_breakdown": _grouped_mission_breakdown(store, account_id),
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
        max_vehicles: int = Form(0),
        own_credit_min: int = Form(0),
        own_credit_max: int = Form(0),
        alliance_credit_min: int = Form(0),
        alliance_credit_max: int = Form(0),
        alliance_min_age_minutes: float = Form(0.0),
        alliance_max_age_minutes: float = Form(0.0),
        max_vehicles_per_mission: str = Form(""),
        order: str = Form("nearest"),
        mission_source: list[str] = Form(default=[]),
        parasite_scope: str = Form("off"),
        parasite_aggressiveness: str = Form("active"),
        top_up_scope: str = Form("off"),
        patient_scope: str = Form("off"),
        hilferuf_mode: str = Form("off"),
        hilferuf_min_credits: int = Form(0),
        parasite_scarce_threshold: int = Form(8),
        partial_starts: str = Form(""),
        batch_size: str = Form(""),
        allowed_categories: str = Form(""),
        alliance_radius_km: str = Form(""),
        transport_enabled: str = Form(""),
        transport_only: str = Form(""),
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
            max_vehicles=max_vehicles,
            own_credit_min=own_credit_min,
            own_credit_max=own_credit_max,
            alliance_credit_min=alliance_credit_min,
            alliance_credit_max=alliance_credit_max,
            alliance_min_age_minutes=alliance_min_age_minutes,
            alliance_max_age_minutes=alliance_max_age_minutes,
            max_vehicles_per_mission=_opt_int(max_vehicles_per_mission),
            order=order,
            mission_source=tuple(mission_source),
            parasite_scope=parasite_scope,
            parasite_aggressiveness=parasite_aggressiveness,
            top_up_scope=top_up_scope,
            patient_scope=patient_scope,
            hilferuf_mode=hilferuf_mode,
            hilferuf_min_credits=hilferuf_min_credits,
            parasite_scarce_threshold=parasite_scarce_threshold,
            partial_starts=bool(partial_starts),
            batch_size=_opt_int(batch_size),
            allowed_categories=tuple(
                c.strip() for c in allowed_categories.split(",") if c.strip()
            ),
            alliance_radius_km=(float(alliance_radius_km) if alliance_radius_km.strip() else None),
            transport_enabled=bool(transport_enabled),
            transport_only=bool(transport_only),
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
        if account_id in orchestrator.group_members:
            # In group mode this account is driven by the group — no solo start.
            return RedirectResponse("/", status_code=303)
        store.set_enabled(account_id, True)
        await orchestrator.start(account_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/accounts/{account_id}/cleanup")
    async def cleanup_account(account_id: int, _=Depends(require_auth)):
        if cleanup is not None:
            try:
                await cleanup(account_id)
            except Exception as exc:  # noqa: BLE001 — surface via the account log
                if log_store is not None:
                    log_store.append(account_id, f"cleanup failed: {exc}")
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


def _combined_totals(store, member_ids) -> dict:
    """Sum dispatched/missions/credits across a set of accounts (group mode's
    combined stats — TODO.md P3.2). Empty dict when there are no members, so
    the template can treat "no group running" and "group with 0 activity" the
    same way (both render nothing)."""
    if not member_ids:
        return {}
    dispatched = missions = credits = 0
    for aid in member_ids:
        t = store.totals(aid)
        dispatched += t.get("dispatched", 0)
        missions += t.get("missions", 0)
        credits += t.get("credits", 0)
    return {"dispatched": dispatched, "missions": missions, "credits": credits}


def _dashboard_stats(store, accounts, color_by_id) -> dict:
    """Headline numbers + a 24-bucket dispatch chart for the dashboard's right
    panel. Credits are ESTIMATED from mission average-credits (no extra polling)."""
    credits = dispatched = missions = 0
    per_account = []
    for account in accounts:
        t = store.totals(account.id)
        credits += t.get("credits", 0)
        dispatched += t.get("dispatched", 0)
        missions += t.get("missions", 0)
        per_account.append(
            {"label": account.label, "color": color_by_id[account.id], "credits": t.get("credits", 0)}
        )
    top_credit = max((p["credits"] for p in per_account), default=0) or 1
    for p in per_account:
        p["pct"] = round(p["credits"] * 100 / top_credit)
        p["credits_fmt"] = f"{p['credits']:,}"
    per_account.sort(key=lambda p: p["credits"], reverse=True)

    series = store.stat_series(None, hours=24)
    peak = max((s["dispatched"] for s in series), default=0) or 1
    bars = [
        {
            "hour": (s["bucket"] or "")[-2:],
            "dispatched": s["dispatched"],
            "credits": s["credits"],
            "pct": max(3, round(s["dispatched"] * 100 / peak)) if s["dispatched"] else 0,
        }
        for s in series
    ]
    return {
        "credits": credits,
        "credits_fmt": f"{credits:,}",
        "dispatched": dispatched,
        "dispatched_fmt": f"{dispatched:,}",
        "missions": missions,
        "missions_fmt": f"{missions:,}",
        "accounts": per_account,
        "bars": bars,
    }
