"""FastAPI app for Meridian Core: routes, session gate and fault injection middleware."""

import asyncio
import hashlib
import os
import re
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from target_app import pages
from target_app.data import ACCOUNT_TYPES, RESTRICTED_MEMBERS, fresh_members
from target_app.state import FaultState, FaultUpdate, SessionStore
from target_app.tenants import TENANTS

COOKIE = "MCSID"
DEFAULT_USER = "demo_operator"
DEFAULT_PASS = "demo-pass-123"
MAIN_FRAME_PREFIXES = ("/welcome", "/inquiry", "/member", "/subacct")
PUBLIC_PREFIXES = ("/login", "/logout", "/__faults", "/img/", "/favicon.ico")
MEMBER_RE = re.compile(r"^\d{5}$")
MAX_DEPOSIT_CENTS = 1_000_000

MSG_INVALID_FORMAT = "INVALID MEMBER NUMBER FORMAT"
MSG_ACCESS_DENIED = "ACCESS DENIED — INSUFFICIENT PRIVILEGE (CODE 403-R)"
MSG_DEPOSIT_RANGE = "DEPOSIT AMOUNT OUT OF RANGE"
MSG_DEPOSIT_INVALID = "INVALID DEPOSIT AMOUNT"
MSG_BAD_TYPE = "INVALID ACCOUNT TYPE"


def msg_not_found(number: str) -> str:
    return f"NO RECORDS FOUND FOR MEMBER NO. {number}"


def is_main_frame(path: str) -> bool:
    return path.startswith(MAIN_FRAME_PREFIXES)


def is_public(path: str) -> bool:
    return path.startswith(PUBLIC_PREFIXES)


async def form_data(request: Request) -> dict[str, str]:
    """Parse an urlencoded body without needing python-multipart."""
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def parse_deposit(raw: str) -> tuple[int | None, str | None]:
    """Return (cents, None) or (None, error message)."""
    try:
        amount = Decimal(raw.strip().replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None, MSG_DEPOSIT_INVALID
    if not amount.is_finite():
        return None, MSG_DEPOSIT_INVALID
    cents = int((amount * 100).to_integral_value())
    if cents <= 0 or cents > MAX_DEPOSIT_CENTS:
        return None, MSG_DEPOSIT_RANGE
    return cents, None


def confirmation_number(member: str, type_code: str, cents: int, nickname: str,
                        counter: int) -> str:
    seed = f"{member}|{type_code}|{cents}|{nickname}|{counter}".encode()
    return f"MC-{int(hashlib.sha256(seed).hexdigest(), 16) % 1_000_000:06d}"


def create_app(
    tenant: str = "a",
    session_ttl: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    if tenant not in TENANTS:
        raise ValueError(f"unknown tenant {tenant!r}; expected one of {sorted(TENANTS)}")
    t = TENANTS[tenant]
    ttl = session_ttl if session_ttl is not None else float(
        os.environ.get("SESSION_TTL_SECONDS", "900")
    )
    operator_user = os.environ.get("BANK_OPERATOR_USER", DEFAULT_USER)
    operator_pass = os.environ.get("BANK_OPERATOR_PASS", DEFAULT_PASS)

    app = FastAPI(title="Meridian Core (mock)", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.tenant = tenant
    app.state.sessions = SessionStore(ttl, clock)
    app.state.faults = FaultState()
    app.state.members = fresh_members()
    app.state.confirm_counter = 0

    def html(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(body, status_code=status)

    def lookup(number: str) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve a member number to (member, None) or (None, business-outcome message)."""
        if not MEMBER_RE.match(number):
            return None, MSG_INVALID_FORMAT
        if number not in app.state.members:
            return None, msg_not_found(number)
        if number in RESTRICTED_MEMBERS:
            return None, MSG_ACCESS_DENIED
        return app.state.members[number], None

    # --- gate: latency -> http500 -> session -> interstitial -------------------------------------

    @app.middleware("http")
    async def legacy_gate(request: Request, call_next):
        path = request.url.path
        if is_public(path):
            return await call_next(request)
        faults: FaultState = app.state.faults
        main = is_main_frame(path)

        if main and faults.latency_count > 0:
            faults.latency_count -= 1
            await asyncio.sleep(faults.latency_ms / 1000)

        if main and faults.http500_count > 0 and path.startswith(faults.http500_path_prefix):
            faults.http500_count -= 1
            return html(pages.system_error(t), 500)

        sid = request.cookies.get(COOKIE)
        session = app.state.sessions.get(sid)
        if session is None:
            if path == "/" and not sid:
                return RedirectResponse("/login", status_code=303)
            return html(pages.session_expired(t))

        if main and request.method == "GET" and faults.interstitial_count > 0:
            faults.interstitial_count -= 1
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return html(pages.interstitial(t, target))

        request.state.session = session
        return await call_next(request)

    # --- fault control (no auth) -----------------------------------------------------------------

    @app.get("/__faults")
    def get_faults() -> JSONResponse:
        return JSONResponse(app.state.faults.model_dump())

    @app.post("/__faults")
    def set_faults(update: FaultUpdate) -> JSONResponse:
        changes = update.model_dump(exclude_none=True, exclude={"expire_session_now"})
        app.state.faults = app.state.faults.model_copy(update=changes)
        if update.expire_session_now:
            app.state.sessions.expire_all()
        return JSONResponse(app.state.faults.model_dump())

    @app.post("/__faults/reset")
    def reset_faults() -> JSONResponse:
        app.state.faults = FaultState()
        return JSONResponse(app.state.faults.model_dump())

    # --- sign on / frames ------------------------------------------------------------------------

    @app.get("/login")
    def login_page() -> HTMLResponse:
        return html(pages.login(t))

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        form = await form_data(request)
        if form.get("f1", "") != operator_user or form.get("f2", "") != operator_pass:
            return html(pages.login(t, "INVALID OPERATOR CREDENTIALS"))
        sid = app.state.sessions.create(operator_user)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
        return response

    @app.get("/logout")
    def logout(request: Request) -> HTMLResponse:
        app.state.sessions.drop(request.cookies.get(COOKIE))
        response = html(pages.logged_out(t))
        response.delete_cookie(COOKIE)
        return response

    @app.get("/")
    def root() -> HTMLResponse:
        return html(pages.frameset(t))

    @app.get("/top")
    def top(request: Request) -> HTMLResponse:
        return html(pages.top(t, request.state.session.operator))

    @app.get("/nav")
    def nav() -> HTMLResponse:
        return html(pages.nav(t))

    @app.get("/welcome")
    def welcome() -> HTMLResponse:
        return html(pages.welcome(t))

    @app.get("/img/btn_submit.svg")
    def submit_image() -> Response:
        return Response(pages.SUBMIT_SVG, media_type="image/svg+xml")

    # --- member inquiry / detail -----------------------------------------------------------------

    @app.get("/inquiry")
    def inquiry(txtA: str | None = None) -> HTMLResponse:
        if txtA is None:
            return html(pages.inquiry(t))
        number = txtA.strip()
        member, problem = lookup(number)
        if problem == MSG_INVALID_FORMAT:
            return html(pages.inquiry(t, txtA, error=problem))
        if problem:
            return html(pages.inquiry(t, txtA, notice=problem))
        return html(pages.inquiry(t, txtA, member=(number, member)))

    @app.get("/member/detail")
    def member_detail(m: str = "") -> HTMLResponse:
        number = m.strip()
        member, problem = lookup(number)
        if problem:
            return html(pages.inquiry(t, number, notice=problem))
        faults: FaultState = app.state.faults
        js_confirm = faults.js_confirm_count > 0
        if js_confirm:
            faults.js_confirm_count -= 1
        return html(pages.member_detail(t, number, member, js_confirm))

    # --- open sub-account ------------------------------------------------------------------------

    def validate_open(member_no: str, type_code: str, amount: str
                      ) -> tuple[dict[str, Any] | None, int | None, str | None]:
        member, problem = lookup(member_no.strip())
        if problem:
            return None, None, problem
        if type_code not in ACCOUNT_TYPES:
            return None, None, MSG_BAD_TYPE
        cents, problem = parse_deposit(amount)
        if problem:
            return None, None, problem
        return member, cents, None

    @app.get("/subacct/open")
    def open_form(m: str = "") -> HTMLResponse:
        return html(pages.open_form(t, {"member": m, "type": "SV"}, ACCOUNT_TYPES))

    @app.post("/subacct/review")
    async def open_review(request: Request) -> HTMLResponse:
        form = await form_data(request)
        values = {
            "member": form.get("txtB", "").strip(),
            "type": form.get("selC", ""),
            "amount": form.get("txtD", ""),
            "nickname": form.get("txtE", "").strip()[:20],
        }
        member, cents, problem = validate_open(values["member"], values["type"], values["amount"])
        if problem:
            return html(pages.open_form(t, values, ACCOUNT_TYPES, error=problem))
        return html(pages.review(
            t, values["member"], member["name"], values["type"],
            ACCOUNT_TYPES[values["type"]]["description"], cents, values["nickname"],
        ))

    @app.post("/subacct/submit")
    async def open_submit(request: Request) -> HTMLResponse:
        form = await form_data(request)
        member_no, type_code = form.get("h1", ""), form.get("h2", "")
        nickname = form.get("h4", "")[:20]
        raw_cents = form.get("h3", "")
        amount = f"{int(raw_cents) / 100:.2f}" if raw_cents.isdigit() else "x"
        member, cents, problem = validate_open(member_no, type_code, amount)
        if problem:
            return html(pages.open_form(
                t, {"member": member_no, "type": type_code, "nickname": nickname},
                ACCOUNT_TYPES, error=problem,
            ))
        app.state.confirm_counter += 1
        conf = confirmation_number(member_no, type_code, cents, nickname,
                                   app.state.confirm_counter)
        used = {int(a["suffix"]) for a in member["accounts"]}
        suffix = f"{next(n for n in range(20, 100, 10) if n not in used):02d}"
        info = ACCOUNT_TYPES[type_code]
        member["accounts"].append({
            "suffix": suffix, "description": info["description"],
            "balance_cents": cents, "rate": info["rate"],
        })
        return html(pages.confirmation(t, conf, member_no, suffix, info["description"], cents))

    return app
