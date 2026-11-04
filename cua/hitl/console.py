"""Operator console: a minimal, server-rendered queue of interventions plus a JSON API.

`cua operator` binds it to 127.0.0.1 only. There is NO authentication and NO audit beyond the
event log. A production deployment needs SSO/auth, per-operator authorization and an
append-only audit trail (out of scope here). Everything shown is already redacted, and
screenshots are masked by GuardedSurface.
"""

import json
from html import escape
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from cua.hitl.interventions import InterventionRequest, InterventionStore, StaleState

Command = Literal["claim", "resume", "handled", "abort", "approve", "deny"]


def _e(v: object) -> str:
    return escape("" if v is None else str(v))


def apply_command(store: InterventionStore, iid: str, cmd: str, operator: str | None,
                  note: str | None) -> InterventionRequest:
    """One place for the rules of each button (shared by the HTML form and the JSON API)."""
    iv = store.get(iid)
    if iv is None:
        raise HTTPException(404, f"no intervention {iid}")
    operator = (operator or "").strip() or None
    try:
        if cmd == "claim":
            if not operator:
                raise HTTPException(400, "take control needs an operator name")
            return store.claim(iid, operator)
        if cmd == "resume":
            return store.resolve(iid, "resumed", operator)
        if cmd == "handled":
            if not (note or "").strip():
                raise HTTPException(400, "mark handled needs a note")
            return store.resolve(iid, "handled", operator, note.strip())
        if cmd == "abort":
            return store.resolve(iid, "aborted", operator, note)
        if cmd in ("approve", "deny"):
            if iv.kind != "approval_required":
                raise HTTPException(400, f"{cmd} only applies to approval_required")
            return store.resolve(iid, "approved" if cmd == "approve" else "denied", operator,
                                 note)
    except StaleState as exc:
        raise HTTPException(409, str(exc)) from None
    raise HTTPException(400, f"unknown command {cmd!r}")


def _within(path: Path, root: Path | None) -> bool:
    try:
        return root is None or path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def create_console(store: InterventionStore, runs_root: Path | None = None) -> FastAPI:
    app = FastAPI(title="CUA operator console", docs_url=None, redoc_url=None,
                  openapi_url=None)

    # --- JSON API --------------------------------------------------------------------------

    @app.get("/api/interventions")
    def api_list(status: str | None = None) -> JSONResponse:
        return JSONResponse([i.model_dump(mode="json") for i in store.list(status)])

    @app.get("/api/interventions/{iid}")
    def api_get(iid: str) -> JSONResponse:
        iv = store.get(iid)
        if iv is None:
            raise HTTPException(404, iid)
        control = store.control(iv.run_id)
        return JSONResponse({**iv.model_dump(mode="json"),
                             "control": control.model_dump(mode="json") if control else None})

    @app.get("/api/control/{run_id}")
    def api_control(run_id: str) -> JSONResponse:
        c = store.control(run_id)
        if c is None:
            raise HTTPException(404, run_id)
        return JSONResponse(c.model_dump(mode="json"))

    @app.post("/api/interventions/{iid}/{cmd}")
    async def api_command(iid: str, cmd: Command, request: Request) -> JSONResponse:
        raw = await request.body()
        body = json.loads(raw) if raw.strip() else {}
        iv = apply_command(store, iid, cmd, body.get("operator"), body.get("note"))
        return JSONResponse(iv.model_dump(mode="json"))

    # --- files (masked screenshots only) ---------------------------------------------------

    def _file(iid: str, which: str) -> FileResponse:
        iv = store.get(iid)
        if iv is None:
            raise HTTPException(404, iid)
        path = Path(iv.screenshot) if which == "screenshot" and iv.screenshot else (
            Path(iv.run_dir) / "live.png" if which == "live" and iv.run_dir else None)
        if path is None or not path.exists() or path.suffix != ".png" \
                or not _within(path, runs_root):
            raise HTTPException(404, f"no {which} image")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "no-store"})

    @app.get("/i/{iid}/screenshot.png")
    def screenshot(iid: str) -> FileResponse:
        return _file(iid, "screenshot")

    @app.get("/i/{iid}/live.png")
    def live(iid: str) -> FileResponse:
        return _file(iid, "live")

    # --- HTML ------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def queue() -> str:
        rows = "".join(
            f"<tr><td><a href='/i/{_e(i.id)}'>{_e(i.id)}</a></td><td>{_e(i.kind)}</td>"
            f"<td>{_e(i.status)}</td><td>{_e(i.capability or i.goal)}</td>"
            f"<td>{_e(i.step_id)}</td><td>{_e(i.reason)[:120]}</td>"
            f"<td>{_e(i.operator)}</td><td>{i.created_at:%H:%M:%S}</td></tr>"
            for i in store.list() if i.status in ("open", "claimed"))
        return _page("Operator queue", f"""<p>Open and claimed interventions
            (refreshes every 2 s). Resolved ones are in <code>cua interventions list</code>.</p>
            <table><tr><th>id</th><th>kind</th><th>status</th><th>capability / goal</th>
            <th>step</th><th>reason</th><th>operator</th><th>created</th></tr>{rows or
            "<tr><td colspan=8>nothing to do</td></tr>"}</table>""", refresh=2)

    @app.get("/i/{iid}", response_class=HTMLResponse)
    def detail(iid: str) -> str:
        iv = store.get(iid)
        if iv is None:
            raise HTTPException(404, iid)
        c = store.control(iv.run_id)
        live_view = iv.live_view
        how = []
        if live_view.headed:
            how.append(f"use the browser window titled <b>{_e(live_view.window_title)}</b>")
        if live_view.cdp_url:
            lead = "or attach" if how else "attach"
            how.append(f"{lead} DevTools: <code>chrome://inspect</code> → configure "
                       f"<code>{_e(live_view.cdp_url.removeprefix('http://'))}</code>, or open "
                       f"<a href='{_e(live_view.cdp_url)}/json'>{_e(live_view.cdp_url)}/json"
                       f"</a> (devtoolsFrontendUrl)")
        actions = "".join(f"<li>{_e(a.summary())}</li>" for a in iv.human_actions)
        frames = "".join(f"<li>{_e(k)}: {_e(v)}</li>" for k, v in iv.frame_urls.items())
        buttons = _buttons(iv)
        live = (f"<h3>Live (masked, every 2 s)</h3><img id=live src='/i/{_e(iv.id)}/live.png'"
                f" width=640 onerror=\"this.style.display='none'\">"
                f"<script>setInterval(()=>{{const i=document.getElementById('live');"
                f"i.style.display='';i.src='/i/{_e(iv.id)}/live.png?t='+Date.now()}},2000)"
                f"</script>") if iv.status == "claimed" else ""
        approval = ""
        if iv.approval:
            d = iv.approval.get("decision", {})
            approval = (f"<h3>Approval requested</h3><p>{_e(iv.approval.get('action'))}</p>"
                        f"<p>risk <b>{_e(d.get('risk'))}</b>, rule {_e(d.get('rule_id'))}: "
                        f"{_e(d.get('reason'))}</p>")
        shot = (f"<h3>At escalation (masked)</h3><img src='/i/{_e(iv.id)}/screenshot.png' "
                f"width=640>") if iv.screenshot else ""
        return _page(f"Intervention {iv.id}", f"""
          <p><a href='/'>&larr; queue</a></p>
          <table>
            <tr><th>kind</th><td>{_e(iv.kind)}</td></tr>
            <tr><th>status</th><td>{_e(iv.status)} {_e(iv.resolution or '')}</td></tr>
            <tr><th>run</th><td>{_e(iv.run_id)}</td></tr>
            <tr><th>capability</th><td>{_e(iv.capability or '-')}</td></tr>
            <tr><th>goal</th><td>{_e(iv.goal or '-')}</td></tr>
            <tr><th>step</th><td>{_e(iv.step_id)} — {_e(iv.step_intent)}</td></tr>
            <tr><th>reason</th><td>{_e(iv.reason)}</td></tr>
            <tr><th>category</th><td>{_e(iv.failure_category)}</td></tr>
            <tr><th>expected</th><td>{_e(iv.expected)}</td></tr>
            <tr><th>observed</th><td>{_e(iv.observed)}</td></tr>
            <tr><th>frames</th><td><ul>{frames}</ul></td></tr>
            <tr><th>control</th><td>{_e(c.state if c else '?')} — owner
              <b>{_e(c.owner if c else '?')}</b>, epoch {_e(c.epoch if c else '?')}</td></tr>
            <tr><th>live session</th><td>{' '.join(how) or 'headless; no live view'}</td></tr>
            <tr><th>operator</th><td>{_e(iv.operator or '-')} {_e(iv.operator_note or '')}</td></tr>
          </table>
          {approval}{buttons}
          <h3>Human actions ({len(iv.human_actions)})</h3><ol>{actions}</ol>
          <p>{iv.ignored_clicks} click(s) on empty areas ignored</p>
          {live}{shot}
          <script>/* reload only when the status changes, so typed fields are kept */
          setInterval(async () => {{
            const r = await fetch('/api/interventions/{_e(iv.id)}');
            if (!r.ok) return;
            const j = await r.json();
            if (j.status !== '{_e(iv.status)}' ||
                j.human_actions.length !== {len(iv.human_actions)} ||
                (j.control && j.control.epoch !== {c.epoch if c else -1})) location.reload();
          }}, 2000);</script>""")

    @app.post("/i/{iid}/{cmd}")
    async def form_command(iid: str, cmd: Command, request: Request) -> RedirectResponse:
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        apply_command(store, iid, cmd, form.get("operator"), form.get("note"))
        return RedirectResponse(f"/i/{iid}", status_code=303)

    return app


def _buttons(iv: InterventionRequest) -> str:
    if iv.status not in ("open", "claimed"):
        return ""

    def form(cmd: str, label: str, fields: str = "") -> str:
        return (f"<form method=post action='/i/{_e(iv.id)}/{cmd}' style='display:inline'>"
                f"{fields}<button>{label}</button></form> ")

    op = "<input name=operator placeholder='operator name' required>"
    note = "<input name=note placeholder='note (required)' required size=40>"
    out = []
    if iv.kind == "approval_required":
        out += [form("approve", "Approve", op), form("deny", "Deny", op)]
    else:
        if iv.status == "open":
            out.append(form("claim", "Take control", op))
        if iv.status == "claimed":
            out.append(form("resume", "Resume"))
        out += [form("handled", "Mark handled", note), form("abort", "Abort")]
    return "<p>" + "".join(out) + "</p>"


def _page(title: str, body: str, refresh: int | None = None) -> str:
    meta = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>{meta}<title>{_e(title)}</title>
<style>body{{font:13px Verdana,sans-serif;margin:16px}} table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}}
th{{background:#eee}} img{{border:1px solid #999}}</style></head>
<body><h2>{_e(title)}</h2>{body}
<p style='color:#888'>Local operator console: 127.0.0.1 only, no auth. Production needs SSO,
authorization and an audit trail.</p></body></html>"""
