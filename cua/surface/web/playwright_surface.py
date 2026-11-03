"""Playwright (sync API) implementation of the Surface interface for web UIs, frames included."""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Dialog,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError

from cua.surface.base import DialogPolicy, Surface, Target
from cua.surface.errors import ActionFailed, NavigationFailed, SettleTimeout, TargetNotFound
from cua.surface.locators import CssPath, Fingerprint, TargetSpec, candidate_strategies, match
from cua.surface.models import (
    TOP_FRAME_KEY,
    ActionResult,
    BBox,
    DialogEvent,
    ElementHandleRef,
    FrameInfo,
    FrameRef,
    Observation,
    ResolveResult,
    ScannedElement,
    StrategyAttempt,
)

RequestFilter = Callable[[str], bool]  # url -> allowed?
BlockedCallback = Callable[[str, str], None]  # (url, resource_type)

SCAN_JS = Path(__file__).with_name("dom_scan.js").read_text()

# Installs (once per document) a MutationObserver that ignores our own data-cua-* tagging.
QUIET_JS = """() => {
  if (!window.__cuaMut) {
    window.__cuaMut = {last: 0};  // quiet clock starts at document start
    new MutationObserver((recs) => {
      const ours = (r) => r.type === 'attributes' && /^data-cua-/.test(r.attributeName || '');
      if (recs.some((r) => !ours(r))) window.__cuaMut.last = performance.now();
    }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  }
  return {ready: document.readyState, quiet_ms: performance.now() - window.__cuaMut.last};
}"""

TEXT_JS = """() => {
  const r = document.body || document.documentElement;
  return r ? r.innerText.replace(/\\s+/g, ' ').trim() : '';
}"""

CSS_JS = """(sel) => Array.from(document.querySelectorAll(sel)).map((el) => {
  window.__cuaTmpSeq = window.__cuaTmpSeq || 0;
  if (!el.hasAttribute('data-cua-tmp')) {
    el.setAttribute('data-cua-tmp', 't' + (++window.__cuaTmpSeq));
  }
  return {handle: el.getAttribute('data-cua-tmp'), ref: el.getAttribute('data-cua-ref')};
})"""


class _SharedDriver:
    """One Playwright driver per thread, ref-counted: the sync API allows only one per thread."""

    _local = threading.local()

    @classmethod
    def acquire(cls) -> Playwright:
        if getattr(cls._local, "count", 0) == 0:
            cls._local.pw = sync_playwright().start()
        cls._local.count = getattr(cls._local, "count", 0) + 1
        return cls._local.pw

    @classmethod
    def release(cls) -> None:
        cls._local.count -= 1
        if cls._local.count == 0:
            cls._local.pw.stop()
            cls._local.pw = None


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__


class PlaywrightSurface(Surface):
    def __init__(
        self,
        *,
        headless: bool = True,
        slow_mo: float = 0,
        default_timeout_ms: float = 10_000,
        settle_timeout_ms: float = 15_000,
        quiet_window_ms: float = 300,
        viewport: tuple[int, int] = (1280, 800),
        trace_path: str | Path | None = None,
        user_data_dir: str | Path | None = None,
        dialog_policy: DialogPolicy | None = None,
        dialog_policy_name: str = "custom",
        max_text_elements: int = 300,
        max_frame_text: int = 4000,
        request_filter: RequestFilter | None = None,
        on_request_blocked: BlockedCallback | None = None,
    ):
        self.headless = headless
        self.slow_mo = slow_mo
        self.default_timeout_ms = default_timeout_ms
        self.settle_timeout_ms = settle_timeout_ms
        self.quiet_window_ms = quiet_window_ms
        self.viewport = viewport
        self.trace_path = Path(trace_path) if trace_path else None
        self._tracing = False
        self._request_filter = request_filter
        self._on_request_blocked = on_request_blocked
        self._route_installed = False
        self.user_data_dir = Path(user_data_dir) if user_data_dir else None
        self.dialog_policy = dialog_policy
        self.dialog_policy_name = dialog_policy_name
        self.max_text_elements = max_text_elements
        self.max_frame_text = max_frame_text

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._dialogs: list[DialogEvent] = []
        self._dialog_cursor = 0
        self._pending_navs: dict[Request, Frame] = {}
        self._nav_count = 0
        self._last: dict[str, ScannedElement] = {}

    # --- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        if self._page is not None:
            return
        self._pw = _SharedDriver.acquire()
        viewport = {"width": self.viewport[0], "height": self.viewport[1]}
        if self.user_data_dir:
            self._context = self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir), headless=self.headless, slow_mo=self.slow_mo,
                viewport=viewport,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
            self._context = self._browser.new_context(viewport=viewport)
            self._page = self._context.new_page()
        self._context.set_default_timeout(self.default_timeout_ms)
        if self.trace_path:
            self.start_trace(self.trace_path)
        if self._request_filter is not None:
            self._install_route()
        page = self._page
        page.on("dialog", self._on_dialog)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)
        page.on("framenavigated", self._on_frame_navigated)

    @property
    def context(self) -> BrowserContext:
        """The live BrowserContext (HITL hands this same session to a human operator)."""
        self.start()
        assert self._context is not None
        return self._context

    @property
    def page(self) -> Page:
        self.start()
        assert self._page is not None
        return self._page

    # --- tracing & network filter ---------------------------------------------------------------

    def start_trace(self, path: str | Path) -> None:
        """Record a Playwright trace.

        Traces hold the UNMASKED DOM and typed values; keep them out of committed evidence.
        """
        self.trace_path = Path(path)
        if not self._tracing:
            self.context.tracing.start(screenshots=True, snapshots=True)
            self._tracing = True

    def stop_trace(self) -> Path | None:
        if not self._tracing or self._context is None or self.trace_path is None:
            return None
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.tracing.stop(path=str(self.trace_path))
        self._tracing = False
        return self.trace_path

    def set_request_filter(self, request_filter: RequestFilter,
                           on_blocked: BlockedCallback | None = None) -> None:
        """Abort any request (navigation, frame, XHR, asset) the filter rejects."""
        self._request_filter = request_filter
        self._on_request_blocked = on_blocked
        if self._context is not None:
            self._install_route()

    def _install_route(self) -> None:
        if not self._route_installed:
            self.context.route("**/*", self._route)
            self._route_installed = True

    def _route(self, route: Route) -> None:
        request = route.request
        try:
            allowed = self._request_filter is None or self._request_filter(request.url)
        except Exception:  # fail closed
            allowed = False
        if allowed:
            route.continue_()
            return
        route.abort("blockedbyclient")
        if self._on_request_blocked is not None:
            self._on_request_blocked(request.url, request.resource_type)

    def open(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="load")
        except PlaywrightError as exc:
            raise NavigationFailed(url, _first_line(exc)) from exc
        self.wait_for_settle()

    def close(self) -> None:
        if self._context is not None:
            self.stop_trace()
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            _SharedDriver.release()
        self._pw = self._browser = self._context = self._page = None
        self._route_installed = False

    def __enter__(self) -> "PlaywrightSurface":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- events ---------------------------------------------------------------------------------

    def _on_dialog(self, dialog: Dialog) -> None:
        decision, handled_by = "dismiss", "default"
        if self.dialog_policy is not None:
            handled_by = f"policy:{self.dialog_policy_name}"
            try:
                decision = self.dialog_policy(dialog.type, dialog.message)
            except Exception:  # a broken policy must not wedge the page
                decision = "dismiss"
        try:
            dialog.accept() if decision == "accept" else dialog.dismiss()
        except PlaywrightError:
            pass
        self._dialogs.append(DialogEvent(
            type=dialog.type, message=dialog.message,
            action_taken="accepted" if decision == "accept" else "dismissed",
            handled_by=handled_by,
        ))

    def _on_request(self, request: Request) -> None:
        try:
            if request.is_navigation_request():
                self._pending_navs[request] = request.frame
        except PlaywrightError:
            pass

    def _on_request_done(self, request: Request) -> None:
        self._pending_navs.pop(request, None)

    def _on_frame_navigated(self, frame: Frame) -> None:
        self._nav_count += 1
        for req, f in list(self._pending_navs.items()):
            if f == frame:
                self._pending_navs.pop(req, None)

    # --- frames ---------------------------------------------------------------------------------

    @staticmethod
    def _child_name(parent: Frame, child: Frame) -> str:
        return child.name or f"@{parent.child_frames.index(child)}"

    def _frame_ref(self, frame: Frame) -> FrameRef:
        names: list[str] = []
        f = frame
        while f.parent_frame is not None:
            names.append(self._child_name(f.parent_frame, f))
            f = f.parent_frame
        path = urlparse(frame.url).path or None
        return FrameRef(name_path=list(reversed(names)), url_path_pattern=path)

    def _frames(self) -> list[Frame]:
        """All attached frames, depth-first in document order, top document first."""
        out: list[Frame] = []

        def walk(f: Frame) -> None:
            if f.is_detached():
                return
            out.append(f)
            for c in f.child_frames:
                walk(c)

        walk(self.page.main_frame)
        return out

    def _frame_for(self, ref: FrameRef) -> Frame | None:
        f = self.page.main_frame
        for name in ref.name_path:
            nxt = next((c for c in f.child_frames if self._child_name(f, c) == name), None)
            if nxt is None:
                return self._frame_by_url(ref)
            f = nxt
        return f

    def _frame_by_url(self, ref: FrameRef) -> Frame | None:
        if not ref.url_path_pattern:
            return None
        hits = [f for f in self._frames() if fnmatch(urlparse(f.url).path, ref.url_path_pattern)]
        return hits[0] if len(hits) == 1 else None

    def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        if frame.parent_frame is None:
            return 0.0, 0.0
        try:
            box = frame.frame_element().bounding_box()
        except PlaywrightError:
            return 0.0, 0.0
        return (box["x"], box["y"]) if box else (0.0, 0.0)

    # --- scanning -------------------------------------------------------------------------------

    def _scan(self, frame: Frame, mode: str, start: int = 0,
              offset: tuple[float, float] = (0.0, 0.0)) -> tuple[list[ScannedElement], dict]:
        raw = frame.evaluate(SCAN_JS, {
            "mode": mode, "start": start, "maxText": self.max_text_elements,
            "maxFrameText": self.max_frame_text,
        })
        fref = self._frame_ref(frame)
        elements = []
        for item in raw["elements"]:
            bbox = item.pop("bbox")
            elements.append(ScannedElement(
                frame=fref,
                bbox=BBox(x=bbox["x"] + offset[0], y=bbox["y"] + offset[1],
                          width=bbox["width"], height=bbox["height"]),
                **item,
            ))
        return elements, raw

    def _css(self, frame: Frame, selector: str) -> list[dict[str, Any]]:
        return frame.evaluate(CSS_JS, selector)

    # --- perceive -------------------------------------------------------------------------------

    def observe(self) -> Observation:
        seq = 0
        frames: list[FrameInfo] = []
        elements: list[ScannedElement] = []
        texts: dict[str, str] = {}
        for frame in self._frames():
            fref = self._frame_ref(frame)
            try:
                scanned, raw = self._scan(frame, "observe", seq, self._frame_offset(frame))
            except PlaywrightError:
                frames.append(FrameInfo(frame=fref, url=frame.url, title="(unavailable)"))
                continue
            seq = raw["next"]
            frames.append(FrameInfo(frame=fref, url=frame.url, title=raw["title"]))
            texts[fref.key] = raw["text"]
            elements += scanned
        self._last = {e.ref: e for e in elements if e.ref}
        dialogs = self._dialogs[self._dialog_cursor:]
        self._dialog_cursor = len(self._dialogs)
        return Observation(
            frames=frames, elements=[e.public() for e in elements], text_by_frame=texts,
            dialogs=dialogs, taken_at=datetime.now(UTC),
        )

    def describe(self, ref: str, description: str | None = None) -> TargetSpec:
        el = self._last.get(ref)
        if el is None:
            raise ActionFailed("describe", ref, "unknown ref; call observe() first")
        frame = self._frame_for(el.frame)
        if frame is None:
            raise ActionFailed("describe", ref, f"frame {el.frame.key} is gone")
        scanned, _ = self._scan(frame, "resolve")
        kept = []
        for strategy in candidate_strategies(el):
            if isinstance(strategy, CssPath):
                refs = [h["ref"] for h in self._css(frame, strategy.selector)]
            else:
                refs = [e.ref for e in match(strategy, scanned)]
            if refs == [ref]:
                kept.append(strategy)
        if not kept:
            raise ActionFailed("describe", ref, "no strategy uniquely identifies the element")
        css_only = all(isinstance(s, CssPath) for s in kept)
        if description is None:
            description = f'{el.role} "{el.name}"'
            if el.label and el.label != el.name:
                description += f" (label {el.label})"
            if el.table_context and el.table_context.table_caption_or_header:
                description += f" in section {el.table_context.table_caption_or_header}"
            description += f" [frame {el.frame.key}]"
        if css_only:
            description += " [BRITTLE: css_path only]"
        return TargetSpec(
            frame=el.frame, strategies=kept, description=description,
            fingerprint=Fingerprint(role=el.role, name=el.name, tag=el.tag, bbox=el.bbox),
        )

    def resolve(self, target: TargetSpec) -> tuple[ElementHandleRef, ResolveResult]:
        frame = self._frame_for(target.frame)
        if frame is None:
            raise TargetNotFound(target, [], note=f"frame {target.frame.key} not found")
        scanned: list[ScannedElement] | None = None
        attempts: list[StrategyAttempt] = []
        for i, strategy in enumerate(target.strategies):
            try:
                if scanned is None:
                    scanned, _ = self._scan(frame, "resolve")
                if isinstance(strategy, CssPath):
                    handles = [h["handle"] for h in self._css(frame, strategy.selector)]
                else:
                    handles = [e.handle for e in match(strategy, scanned)]
            except PlaywrightError as exc:
                attempts.append(StrategyAttempt(kind=strategy.kind, match_count=0,
                                                outcome="error", note=_first_line(exc)))
                continue
            n = len(handles)
            if n == 1:
                attempts.append(StrategyAttempt(kind=strategy.kind, match_count=1,
                                                outcome="matched"))
                info = next((e.public() for e in scanned or [] if e.handle == handles[0]), None)
                return (
                    ElementHandleRef(frame=target.frame,
                                     selector=f'[data-cua-tmp="{handles[0]}"]', element=info),
                    ResolveResult(matched=True, winning_strategy_index=i, degraded=i > 0,
                                  attempts=attempts),
                )
            attempts.append(StrategyAttempt(
                kind=strategy.kind, match_count=n,
                outcome="not_found" if n == 0 else "ambiguous",
                note="" if n == 0 else f"{n} elements matched",
            ))
        raise TargetNotFound(target, attempts)

    # --- act ------------------------------------------------------------------------------------

    def _locate(self, action: str, target: Target) -> tuple[Locator, ResolveResult, str]:
        if isinstance(target, str):
            el = self._last.get(target)
            if el is None:
                raise ActionFailed(action, target, "unknown ref; call observe() first")
            frame = self._frame_for(el.frame)
            desc = f'{target} {el.role} "{el.name}"'
            if frame is None:
                raise ActionFailed(action, desc, f"frame {el.frame.key} is gone")
            loc = frame.locator(f'[data-cua-ref="{target}"]')
            if loc.count() != 1:
                raise ActionFailed(action, desc, "stale ref: page changed since observe()")
            return loc, ResolveResult(matched=True), desc
        handle, result = self.resolve(target)
        frame = self._frame_for(handle.frame)
        assert frame is not None
        return frame.locator(handle.selector), result, target.description

    def _act(self, action: str, target: Target | None, fn: Callable[[Locator | None], None],
             settle: bool, secret: str | None = None) -> ActionResult:
        started = time.perf_counter()
        dialogs_before, navs_before = len(self._dialogs), self._nav_count
        loc, result, desc = (None, None, "page") if target is None else self._locate(action, target)
        try:
            fn(loc)
        except PlaywrightError as exc:
            reason = _first_line(exc)
            if secret:
                reason = reason.replace(secret, "***")
            raise ActionFailed(action, desc, reason) from exc
        if settle:
            self.wait_for_settle()
        return ActionResult(
            ok=True, resolve_result=result, dialogs=self._dialogs[dialogs_before:],
            navigated=self._nav_count != navs_before,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    def click(self, target: Target, *, settle: bool = True) -> ActionResult:
        return self._act("click", target, lambda loc: loc.click(), settle)

    def fill(self, target: Target, value: str, *, settle: bool = True) -> ActionResult:
        return self._act("fill", target, lambda loc: loc.fill(value), settle, secret=value)

    def select(self, target: Target, option_label: str, *, settle: bool = True) -> ActionResult:
        return self._act("select", target, lambda loc: loc.select_option(label=option_label),
                         settle)

    def press(self, key: str, target: Target | None = None, *, settle: bool = True
              ) -> ActionResult:
        def do(loc: Locator | None) -> None:
            if loc is None:
                self.page.keyboard.press(key)
            else:
                loc.press(key)

        return self._act("press", target, do, settle)

    # --- read -----------------------------------------------------------------------------------

    def read_text(self, target: Target) -> str:
        loc, _, desc = self._locate("read_text", target)
        try:
            return " ".join(loc.inner_text().split())
        except PlaywrightError as exc:
            raise ActionFailed("read_text", desc, _first_line(exc)) from exc

    def frame_text(self, frame: FrameRef | None = None) -> str:
        if frame is not None:
            f = self._frame_for(frame)
            if f is None:
                raise ActionFailed("frame_text", frame.key, "frame not found")
            return f.evaluate(TEXT_JS)
        return "\n".join(t for f in self._frames() if (t := f.evaluate(TEXT_JS)))

    def page_contains(self, text: str, frame: FrameRef | None = None) -> bool:
        return " ".join(text.split()) in self.frame_text(frame)

    def screenshot(self, path: str | Path, mask_targets: list[TargetSpec] | None = None) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        masks = []
        for target in mask_targets or []:
            handle, _ = self.resolve(target)
            frame = self._frame_for(handle.frame)
            assert frame is not None
            masks.append(frame.locator(handle.selector))
        self.page.screenshot(path=str(path), mask=masks)
        return path

    def current_urls(self) -> dict[str, str]:
        return {self._frame_ref(f).key: f.url for f in self._frames()}

    # --- settle ---------------------------------------------------------------------------------

    def _busy(self) -> tuple[str, str] | None:
        for req, frame in list(self._pending_navs.items()):
            if frame.is_detached():
                self._pending_navs.pop(req, None)
                continue
            return self._frame_ref(frame).key, f"navigation pending: {urlparse(req.url).path}"
        for frame in self._frames():
            try:
                state = frame.evaluate(QUIET_JS)
            except PlaywrightError:
                return self._frame_ref(frame).key, "execution context unavailable (navigating)"
            if state["ready"] != "complete":
                return self._frame_ref(frame).key, f"readyState={state['ready']}"
            if state["quiet_ms"] < self.quiet_window_ms:
                return self._frame_ref(frame).key, "DOM still mutating"
        return None

    def wait_for_settle(self, timeout_ms: float | None = None) -> None:
        timeout_ms = timeout_ms if timeout_ms is not None else self.settle_timeout_ms
        start = time.perf_counter()
        deadline = start + timeout_ms / 1000
        last_busy = (TOP_FRAME_KEY, "quiet window not yet elapsed")
        while True:
            busy = self._busy()
            # Watch for at least one quiet window even if idle, so that navigations scheduled
            # by the last action (form.submit() from a javascript: link) have time to start.
            if busy is None and (time.perf_counter() - start) * 1000 >= self.quiet_window_ms:
                return
            if busy is not None:
                last_busy = busy
            if time.perf_counter() >= deadline:
                raise SettleTimeout(last_busy[0], last_busy[1], timeout_ms)
            self.page.wait_for_timeout(50)
