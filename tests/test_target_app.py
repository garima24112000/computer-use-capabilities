"""Behavioral tests for the Meridian Core mock legacy app (target_app)."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from target_app.app import DEFAULT_PASS, DEFAULT_USER, confirmation_number, create_app


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _default_creds(monkeypatch):
    monkeypatch.delenv("BANK_OPERATOR_USER", raising=False)
    monkeypatch.delenv("BANK_OPERATOR_PASS", raising=False)


def make_client(tenant: str = "a", clock: FakeClock | None = None, ttl: float = 900) -> TestClient:
    app = create_app(tenant, session_ttl=ttl, clock=clock or FakeClock())
    return TestClient(app, follow_redirects=False)


def login(client: TestClient) -> None:
    r = client.post("/login", data={"f1": DEFAULT_USER, "f2": DEFAULT_PASS})
    assert r.status_code == 303
    assert "MCSID" in r.cookies


@pytest.fixture
def client() -> TestClient:
    c = make_client()
    login(c)
    return c


def open_subaccount(client: TestClient, member="12345", type_code="MM", amount="250.00",
                    nickname="RAINY DAY"):
    return client.post(
        "/subacct/review",
        data={"txtB": member, "selC": type_code, "txtD": amount, "txtE": nickname},
    )


# --- sign on / frames ----------------------------------------------------------------------------


def test_login_success_redirects_to_frameset():
    c = make_client()
    r = c.post("/login", data={"f1": DEFAULT_USER, "f2": DEFAULT_PASS})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_failure_shows_invalid_credentials():
    c = make_client()
    r = c.post("/login", data={"f1": DEFAULT_USER, "f2": "wrong"})
    assert r.status_code == 200
    assert "INVALID OPERATOR CREDENTIALS" in r.text
    assert "MCSID" not in r.cookies


def test_login_uses_env_credentials(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "op2")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "pw2")
    c = make_client()
    assert "INVALID" in c.post("/login", data={"f1": DEFAULT_USER, "f2": DEFAULT_PASS}).text
    assert c.post("/login", data={"f1": "op2", "f2": "pw2"}).status_code == 303


def test_root_without_session_redirects_to_login():
    r = make_client().get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_frameset_served(client):
    r = client.get("/")
    assert "<frameset" in r.text
    for name in ("top", "nav", "main"):
        assert f'name="{name}"' in r.text
    assert "DEMO_OPERATOR" in client.get("/top").text
    nav = client.get("/nav").text
    assert "MEMBER INQUIRY" in nav and "OPEN SUB-ACCOUNT" in nav and "REPORTS" in nav


def test_logout_ends_session(client):
    client.get("/logout")
    assert "SESSION EXPIRED" in client.get("/inquiry").text


@pytest.mark.parametrize("path", ["/login", "/", "/top", "/nav", "/inquiry?txtA=12345",
                                  "/member/detail?m=12345", "/subacct/open?m=12345"])
def test_pages_are_hostile_no_ids_testids_or_aria(client, path):
    html = client.get(path).text
    assert not re.search(r"\sid\s*=", html, re.I)
    assert "data-testid" not in html.lower()
    assert "aria-" not in html.lower()
    assert "<label" not in html.lower()


# --- session expiry ------------------------------------------------------------------------------


def test_idle_session_expires_with_200_page():
    clock = FakeClock()
    c = make_client(clock=clock, ttl=60)
    login(c)
    assert "MEMBER INQUIRY" in c.get("/inquiry").text
    clock.now += 61
    r = c.get("/inquiry")
    assert r.status_code == 200
    assert "SESSION EXPIRED — PLEASE SIGN ON AGAIN" in r.text
    assert 'href="/login"' in r.text


# --- inquiry & business outcomes -----------------------------------------------------------------


def test_inquiry_happy_path(client):
    r = client.get("/inquiry", params={"txtA": "12345"})
    assert "SAMPLE, JOHN Q" in r.text and "001 - MAIN OFFICE" in r.text
    assert "openMbr('12345')" in r.text


def test_member_detail(client):
    r = client.get("/member/detail", params={"m": "12345"})
    assert "900-12-3456" in r.text
    assert "john.sample@example.com" in r.text and "(555) 555-0101" in r.text
    assert re.search(r"Share Savings</td><td[^>]*>\$12,345\.67", r.text)
    assert re.search(r"Checking</td><td[^>]*>\$2,500\.00", r.text)
    assert "OPEN SUB-ACCOUNT" in r.text
    assert "window.confirm" not in r.text


def test_member_not_found(client):
    r = client.get("/inquiry", params={"txtA": "99999"})
    assert "NO RECORDS FOUND FOR MEMBER NO. 99999" in r.text


@pytest.mark.parametrize("value", ["1234", "123456", "abcde", "", "12 45"])
def test_invalid_member_format(client, value):
    r = client.get("/inquiry", params={"txtA": value})
    assert "INVALID MEMBER NUMBER FORMAT" in r.text
    assert "#CC0000" in r.text


@pytest.mark.parametrize("path", ["/inquiry?txtA=40300", "/member/detail?m=40300"])
def test_restricted_member_access_denied(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "ACCESS DENIED — INSUFFICIENT PRIVILEGE (CODE 403-R)" in r.text
    assert "900-40-3000" not in r.text


# --- open sub-account ----------------------------------------------------------------------------


def test_open_subaccount_review_and_submit(client):
    r = open_subaccount(client)
    assert "OPEN SUB-ACCOUNT - REVIEW" in r.text
    assert "Money Market" in r.text and "$250.00" in r.text and "RAINY DAY" in r.text
    assert 'type="image"' in r.text and "CANCEL" in r.text

    r = client.post("/subacct/submit",
                    data={"h1": "12345", "h2": "MM", "h3": "25000", "h4": "RAINY DAY"})
    expected = confirmation_number("12345", "MM", 25000, "RAINY DAY", 1)
    assert f"SUB-ACCOUNT OPENED — CONFIRMATION NO. {expected}" in r.text
    assert re.fullmatch(r"MC-\d{6}", expected)

    detail = client.get("/member/detail", params={"m": "12345"}).text
    assert re.search(r"<td>20</td><td>Money Market</td><td[^>]*>\$250\.00", detail)


def test_confirmation_number_is_deterministic():
    a = confirmation_number("12345", "SV", 100, "", 1)
    assert a == confirmation_number("12345", "SV", 100, "", 1)
    assert a != confirmation_number("12345", "SV", 100, "", 2)


@pytest.mark.parametrize("amount", ["0", "-5", "10000.01", "50000"])
def test_deposit_out_of_range(client, amount):
    r = open_subaccount(client, amount=amount)
    assert "DEPOSIT AMOUNT OUT OF RANGE" in r.text
    assert 'name="frmO"' in r.text  # stays on the form


def test_deposit_upper_bound_inclusive(client):
    assert "REVIEW" in open_subaccount(client, amount="$10,000.00").text


def test_open_subaccount_restricted_member(client):
    assert "ACCESS DENIED" in open_subaccount(client, member="40300").text


# --- faults --------------------------------------------------------------------------------------


def test_faults_get_set_reset():
    c = make_client()
    assert c.get("/__faults").json()["latency_count"] == 0
    state = c.post("/__faults", json={"latency_ms": 10, "latency_count": 2}).json()
    assert state["latency_ms"] == 10 and state["latency_count"] == 2
    # partial update keeps other fields
    assert c.post("/__faults", json={"interstitial_count": 1}).json()["latency_count"] == 2
    reset = c.post("/__faults/reset").json()
    assert reset["latency_count"] == 0 and reset["interstitial_count"] == 0


def test_faults_reject_negative_counts():
    assert make_client().post("/__faults", json={"latency_count": -1}).status_code == 422


def test_fault_latency(client):
    client.post("/__faults", json={"latency_ms": 300, "latency_count": 1})
    client.get("/nav")  # non-main-frame requests do not consume the counter
    assert client.get("/__faults").json()["latency_count"] == 1
    start = time.perf_counter()
    client.get("/inquiry")
    assert time.perf_counter() - start >= 0.3
    assert client.get("/__faults").json()["latency_count"] == 0
    start = time.perf_counter()
    client.get("/inquiry")
    assert time.perf_counter() - start < 0.3


def test_fault_interstitial_then_continue(client):
    client.post("/__faults", json={"interstitial_count": 1})
    r = client.get("/inquiry", params={"txtA": "12345"})
    assert "SYSTEM NOTICE — Scheduled maintenance tonight 11PM." in r.text
    assert "[CONTINUE]" in r.text and "/inquiry?txtA=12345" in r.text
    assert "SAMPLE, JOHN Q" in client.get("/inquiry", params={"txtA": "12345"}).text


def test_fault_js_confirm_once(client):
    client.post("/__faults", json={"js_confirm_count": 1})
    first = client.get("/member/detail", params={"m": "12345"}).text
    assert 'window.confirm("Unposted items exist. Continue?")' in first
    second = client.get("/member/detail", params={"m": "12345"}).text
    assert "window.confirm" not in second


def test_fault_expire_session_now(client):
    client.post("/__faults", json={"expire_session_now": True})
    r = client.get("/inquiry")
    assert r.status_code == 200 and "SESSION EXPIRED — PLEASE SIGN ON AGAIN" in r.text
    assert client.get("/__faults").json().get("expire_session_now") is None


def test_fault_http500_with_prefix(client):
    client.post("/__faults", json={"http500_count": 1, "http500_path_prefix": "/member"})
    assert client.get("/inquiry").status_code == 200  # prefix does not match
    r = client.get("/member/detail", params={"m": "12345"})
    assert r.status_code == 500
    assert "SYSTEM ERROR 0xC0FFEE — CONTACT ADMINISTRATOR" in r.text
    assert client.get("/member/detail", params={"m": "12345"}).status_code == 200


# --- tenants -------------------------------------------------------------------------------------


def test_tenant_b_labels_and_layout():
    c = make_client("b")
    login(c)
    assert "PRAIRIE HARVEST" in c.get("/top").text
    inquiry = c.get("/inquiry").text
    assert "ACCT HOLDER #" in inquiry and "SEARCH" in inquiry
    assert "MEMBER NO." not in inquiry and "INQUIRE" not in inquiry
    nav = c.get("/nav").text
    assert nav.index("OPEN SUB-ACCOUNT") < nav.index("MEMBER INQUIRY")
    detail = c.get("/member/detail", params={"m": "12345"}).text
    assert "<b>RATE</b>" in detail and "0.25%" in detail
    assert "$12,345.67" in detail


def test_tenant_a_has_no_rate_column(client):
    assert "<b>RATE</b>" not in client.get("/member/detail", params={"m": "12345"}).text


def test_unknown_tenant_rejected():
    with pytest.raises(ValueError):
        create_app("z")
