"""Human action capture in the live browser session.

An init script (added to the BrowserContext, so every frame of every new document gets it)
listens for click / change / submit in the capture phase. It describes the target element
with the SAME logic as dom_scan.js (it embeds the scan and calls it with `only: element`). It
reports through `context.expose_binding("__cuaHuman")`. Frame navigations come from
Playwright's framenavigated event, as path only.

Values: the script never sends the value of a password field, or of a field whose label is
sensitive (Python decides the label part). Other values pass through the Redactor before they
are stored. Capture is a no-op unless the control state is HUMAN.

Callbacks are delivered in the run thread while it waits with page.wait_for_timeout(), because
Playwright's sync API dispatches events only while it is pumping.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from cua.hitl.interventions import HumanAction
from cua.surface.web.playwright_surface import SCAN_JS

CAPTURE_JS = """(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const SCAN = __SCAN_JS__;
  const describe = (el) => {
    try {
      const r = SCAN({mode: 'resolve', only: el, maxText: 1, maxFrameText: 0});
      return r.elements[0] || null;
    } catch (e) { return null; }
  };
  const send = (payload) => { try { window.__cuaHuman(payload); } catch (e) {} };
  document.addEventListener('click', (e) => {
    const el = e.target && e.target.nodeType === 1 ? e.target : null;
    if (!el || el.closest('[data-cua-ignore]')) return;
    send({type: 'click', el: describe(el)});
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    const rec = describe(el);
    if (el.tagName === 'SELECT') {
      const opt = el.options[el.selectedIndex];
      send({type: 'select', el: rec, option: opt ? opt.text.trim() : ''});
      return;
    }
    const secret = (el.type || '').toLowerCase() === 'password';
    send({type: 'input', el: rec, password: secret, value: secret ? null : el.value});
  }, true);
  document.addEventListener('submit', (e) => {
    send({type: 'submit', el: describe(e.target)});
  }, true);
})()""".replace("__SCAN_JS__", SCAN_JS.strip())

Sink = Callable[[HumanAction], None]


class HumanCapture:
    """Wires the init script, binding and navigation listener to `sink` (a HumanAction per
    event). `active()` gates everything: nothing is recorded unless a human holds control."""

    def __init__(self, surface: Any, redactor: Any, classify: Callable[[str, str, str], str],
                 active: Callable[[], bool], operator: Callable[[], str], sink: Sink):
        self.surface = surface  # PlaywrightSurface (raw: capture must not go through policy)
        self.redactor = redactor
        self.classify = classify  # (role, name, frame_key) -> risk
        self.active = active
        self.operator = operator
        self.sink = sink
        self._seq = 0
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        ctx = self.surface.context
        ctx.expose_binding("__cuaHuman", self._on_binding)
        ctx.add_init_script(script=CAPTURE_JS)
        self.surface.page.on("framenavigated", self._on_nav)
        self._installed = True

    def arm_current_documents(self) -> None:
        """Init scripts only run for NEW documents; arm the ones already loaded."""
        for frame in self.surface.frames():
            try:
                frame.evaluate(CAPTURE_JS)
            except Exception:
                pass

    # --- events ---------------------------------------------------------------------------------

    def _emit(self, **fields: Any) -> None:
        self._seq += 1
        self.sink(HumanAction(seq=self._seq, at=datetime.now(UTC), operator=self.operator(),
                              **fields))

    def _frame_key(self, frame: Any) -> str:
        try:
            return self.surface.frame_key(frame)
        except Exception:
            return "?"

    def _on_binding(self, source: dict, payload: Any) -> None:
        if not self.active() or not isinstance(payload, dict):
            return
        frame = self._frame_key(source.get("frame"))
        el = payload.get("el") or {}
        role, name = el.get("role"), self.redactor.redact_text(el.get("name") or "")
        label = el.get("label") or el.get("name") or ""
        kind = payload.get("type")
        if kind == "click":
            risk = self.classify(role or "", el.get("name") or "", frame)
            self._emit(type="click", frame=frame, role=role, name=name, risk=risk,
                       human_irreversible=risk == "irreversible")
        elif kind == "select":
            self._emit(type="select", frame=frame, role=role, name=name,
                       detail=f"option {self.redactor.redact_text(payload.get('option', ''))!r}")
        elif kind == "input":
            sensitive = payload.get("password") or self.redactor.labels.kind_for(label)
            value = payload.get("value")
            if sensitive or value is None:
                detail = "value changed"  # never the value of a password/sensitive field
            else:
                detail = f"value changed to {self.redactor.redact_text(str(value))!r}"
            self._emit(type="input", frame=frame, role=role, name=name, detail=detail)
        elif kind == "submit":
            self._emit(type="submit", frame=frame, role=role, name=name)

    def _on_nav(self, frame: Any) -> None:
        if not self.active():
            return
        path = urlsplit(frame.url).path
        self._emit(type="navigate", frame=self._frame_key(frame), detail=path)


def dumps(actions: list[HumanAction]) -> str:
    return json.dumps([a.model_dump(mode="json") for a in actions])
