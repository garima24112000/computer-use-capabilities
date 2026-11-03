"""End-to-end tests of PlaywrightSurface against Meridian Core in headless Chromium."""

import time

import pytest

from cua.surface import (
    LabelAnchor,
    RoleName,
    SettleTimeout,
    TableCell,
    TargetNotFound,
    TextMatch,
)
from tests.surface.conftest import set_faults
from tests.surface.helpers import MAIN, TOP, goto_inquiry, inquire, open_detail, spec

SECRET = "SECRET-98765"


def kinds(target) -> list[str]:
    return [s.kind for s in target.strategies]


def find(obs, role: str, name: str, frame_key: str = "main"):
    hits = [e for e in obs.elements
            if e.role == role and e.name == name and e.frame.key == frame_key]
    assert len(hits) == 1, f"expected one {role} {name!r} in {frame_key}, got {len(hits)}"
    return hits[0]


# --- observation ---------------------------------------------------------------------------------


def test_observation_on_inquiry_page(surface):
    goto_inquiry(surface)
    obs = surface.observe()
    box = find(obs, "textbox", "MEMBER NO.")
    assert box.frame.name_path == ["main"]
    assert box.label == "MEMBER NO." and box.has_value is False
    # INQUIRE is <a href="javascript:...">, so its computed role is link (see design notes).
    find(obs, "link", "INQUIRE")
    find(obs, "button", "MEMBER INQUIRY", "nav")
    refs = [e.ref for e in obs.elements]
    assert len(refs) == len(set(refs)) and all(refs)
    assert {f.frame.key for f in obs.frames} == {"_top", "top", "nav", "main"}
    assert "MEMBER INQUIRY" in obs.text_by_frame["main"]


def test_observation_never_contains_field_values(surface):
    goto_inquiry(surface)
    ref = find(surface.observe(), "textbox", "MEMBER NO.").ref
    surface.fill(ref, SECRET)
    obs = surface.observe()
    assert find(obs, "textbox", "MEMBER NO.").has_value is True
    assert SECRET not in obs.model_dump_json()
    assert SECRET not in obs.to_prompt_text()


def test_prompt_text_is_deterministic(surface):
    goto_inquiry(surface)
    first = surface.observe().to_prompt_text()
    second = surface.observe().to_prompt_text()
    assert first == second
    assert '[e' in first and 'textbox "MEMBER NO." (label: MEMBER NO.) frame=main' in first


# --- describe / resolve --------------------------------------------------------------------------


def test_describe_member_no_textbox(surface):
    goto_inquiry(surface)
    ref = find(surface.observe(), "textbox", "MEMBER NO.").ref
    target = surface.describe(ref)
    k = kinds(target)
    assert k[:2] == ["role_name", "label_anchor"] and k[-1] == "css_path"
    # TextMatch("MEMBER NO.") also hits the label cell, so validation must have dropped it.
    assert "text_match" not in k
    assert target.frame.name_path == ["main"]
    assert target.fingerprint.role == "textbox" and target.fingerprint.name == "MEMBER NO."
    assert "data-cua" not in target.model_dump_json()
    assert "BRITTLE" not in target.description


def test_resolve_after_reload_finds_same_element(surface):
    goto_inquiry(surface)
    obs = surface.observe()
    target = surface.describe(find(obs, "textbox", "MEMBER NO.").ref)

    goto_inquiry(surface)  # reloads the main frame: new document, refs regenerated
    surface.observe()
    handle, result = surface.resolve(target)
    assert result.matched and result.winning_strategy_index == 0 and not result.degraded
    assert handle.element.role == "textbox" and handle.element.name == "MEMBER NO."
    assert surface.fill(target, "12345").resolve_result.degraded is False


def test_ambiguous_role_name_falls_through_to_label_anchor(make_surface):
    s = make_surface()
    s.page.set_content("""<table>
      <tr><td>APPROVE</td><td><button>OK</button></td></tr>
      <tr><td>REJECT</td><td><button>OK</button></td></tr></table>""")
    obs = s.observe()
    oks = [e for e in obs.elements if e.role == "button" and e.name == "OK"]
    assert len(oks) == 2

    described = s.describe(oks[1].ref)
    assert kinds(described) == ["label_anchor", "css_path"]

    target = spec(TOP, RoleName(role="button", name="OK"),
                  LabelAnchor(label="REJECT", role="button"))
    handle, result = s.resolve(target)
    assert result.matched and result.winning_strategy_index == 1 and result.degraded
    assert [(a.kind, a.outcome, a.match_count) for a in result.attempts] == [
        ("role_name", "ambiguous", 2), ("label_anchor", "matched", 1)]
    assert handle.element.label == "REJECT"


def test_fully_ambiguous_raises_with_attempts(make_surface):
    s = make_surface()
    s.page.set_content("<div><button>OK</button></div><div><button>OK</button></div>")
    obs = s.observe()
    first = next(e for e in obs.elements if e.name == "OK")
    described = s.describe(first.ref)
    assert kinds(described) == ["css_path"] and "BRITTLE" in described.description

    with pytest.raises(TargetNotFound) as exc:
        s.resolve(spec(TOP, RoleName(role="button", name="OK"), TextMatch(text="OK")))
    assert [(a.kind, a.outcome, a.match_count) for a in exc.value.attempts] == [
        ("role_name", "ambiguous", 2), ("text_match", "ambiguous", 2)]
    assert "expected" in str(exc.value) and "observed" in str(exc.value)


# --- reading -------------------------------------------------------------------------------------


def test_table_cell_reads_share_savings_balance(surface):
    open_detail(surface, "12345")
    target = spec(MAIN, TableCell(section_header="ACCOUNTS", row_key="Share Savings",
                                  column_header="BALANCE"))
    assert surface.read_text(target) == "$12,345.67"

    obs = surface.observe()
    balance = find(obs, "cell", "$12,345.67")
    assert balance.table_context.table_caption_or_header == "ACCOUNTS"
    assert balance.table_context.column_header == "BALANCE"
    assert balance.table_context.row_key == "00"
    described = surface.describe(balance.ref)
    assert "table_cell" in kinds(described)


def test_frame_helpers_and_screenshot_mask(surface, tmp_path):
    open_detail(surface, "12345")
    urls = surface.current_urls()
    assert urls["main"].endswith("/member/detail?m=12345") and urls["nav"].endswith("/nav")
    assert surface.page_contains("ACCOUNTS", MAIN)
    assert not surface.page_contains("ACCOUNTS", TOP)
    ssn = spec(MAIN, RoleName(role="cell", name="900-12-3456"))
    out = surface.screenshot(tmp_path / "detail.png", mask_targets=[ssn])
    assert out.exists() and out.stat().st_size > 0


# --- drift ---------------------------------------------------------------------------------------


def test_tenant_a_target_on_tenant_b_is_degraded_not_faked(surface, surface_b):
    """Recorded on A ("INQUIRE"), replayed on B ("SEARCH").

    Observed behaviour: RoleName and TextMatch miss, and the structural CssPath still finds the
    (renamed) control, so we get a *degraded* match. This is the multi-tenant drift signal.
    """
    goto_inquiry(surface)
    target = surface.describe(find(surface.observe(), "link", "INQUIRE").ref)
    assert kinds(target) == ["role_name", "label_anchor", "text_match", "css_path"]

    goto_inquiry(surface_b)
    handle, result = surface_b.resolve(target)
    assert result.matched and result.degraded
    assert target.strategies[result.winning_strategy_index].kind == "css_path"
    assert [(a.kind, a.outcome) for a in result.attempts] == [
        ("role_name", "not_found"), ("label_anchor", "not_found"), ("text_match", "not_found"),
        ("css_path", "matched")]
    assert handle.element is not None and handle.element.name == "SEARCH"


# --- dialogs -------------------------------------------------------------------------------------


def test_js_confirm_dismissed_by_default(surface, app_a):
    set_faults(app_a, js_confirm_count=1)
    result = open_detail(surface)
    assert [(d.type, d.message, d.action_taken, d.handled_by) for d in result.dialogs] == [
        ("confirm", "Unposted items exist. Continue?", "dismissed", "default")]
    obs = surface.observe()
    assert [d.action_taken for d in obs.dialogs] == ["dismissed"]
    # The app's own handler sends a dismissed confirm back to the inquiry page.
    assert "MEMBER INQUIRY" in obs.text_by_frame["main"]
    assert surface.observe().dialogs == []  # drained


def test_js_confirm_accepted_by_policy(app_a, make_surface):
    from tests.surface.helpers import login

    def policy(kind: str, message: str):
        return "accept" if message.startswith("Unposted items exist") else "dismiss"

    s = make_surface(dialog_policy=policy, dialog_policy_name="known-dialogs")
    login(s, app_a)
    set_faults(app_a, js_confirm_count=1)
    result = open_detail(s)
    assert [(d.action_taken, d.handled_by) for d in result.dialogs] == [
        ("accepted", "policy:known-dialogs")]
    assert s.page_contains("MEMBER DETAIL - 12345", MAIN)


# --- settle --------------------------------------------------------------------------------------


def _ready_to_inquire(s, member="12345"):
    goto_inquiry(s)
    s.fill(spec(MAIN, LabelAnchor(label="MEMBER NO.", role="textbox")), member)
    return spec(MAIN, RoleName(role="link", name="INQUIRE"))


def test_settle_waits_out_latency(surface, app_a):
    inquire_btn = _ready_to_inquire(surface)
    set_faults(app_a, latency_ms=2000, latency_count=1)
    started = time.perf_counter()
    surface.click(inquire_btn, settle=False)
    surface.wait_for_settle(8000)
    assert time.perf_counter() - started >= 1.8
    assert surface.page_contains("SAMPLE, JOHN Q", MAIN)


def test_settle_timeout_names_busy_frame(surface, app_a):
    inquire_btn = _ready_to_inquire(surface)
    set_faults(app_a, latency_ms=2000, latency_count=1)
    surface.click(inquire_btn, settle=False)
    with pytest.raises(SettleTimeout) as exc:
        surface.wait_for_settle(500)
    assert exc.value.frame_key == "main"
    assert "navigation pending" in exc.value.reason
    surface.wait_for_settle(8000)  # let the slow page finish before teardown


def test_inquire_helper_reaches_results(surface):
    inquire(surface, "99999")
    assert surface.page_contains("NO RECORDS FOUND FOR MEMBER NO. 99999", MAIN)
