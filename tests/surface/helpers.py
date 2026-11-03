"""Shared helpers: drive Meridian Core through the Surface API only (TargetSpecs, no Playwright)."""

from cua.surface import FrameRef, LabelAnchor, RoleName, Surface, TableCell, TargetSpec

TOP = FrameRef()
NAV = FrameRef(name_path=["nav"])
MAIN = FrameRef(name_path=["main"])
USER, PASSWORD = "demo_operator", "demo-pass-123"


def spec(frame: FrameRef, *strategies, description: str = "test target") -> TargetSpec:
    return TargetSpec(frame=frame, strategies=list(strategies), description=description)


def login(s: Surface, base_url: str) -> None:
    s.open(f"{base_url}/login")
    s.fill(spec(TOP, LabelAnchor(label="OPERATOR ID", role="textbox")), USER)
    s.fill(spec(TOP, LabelAnchor(label="PASSWORD", role="textbox")), PASSWORD)
    s.click(spec(TOP, RoleName(role="button", name="SIGN ON")))
    assert s.page_contains("SELECT A FUNCTION FROM THE MENU", MAIN)


def goto_inquiry(s: Surface) -> None:
    s.click(spec(NAV, RoleName(role="button", name="MEMBER INQUIRY")))


def inquire(s: Surface, member: str, label: str = "MEMBER NO.", button: str = "INQUIRE") -> None:
    goto_inquiry(s)
    s.fill(spec(MAIN, LabelAnchor(label=label, role="textbox")), member)
    s.click(spec(MAIN, RoleName(role="link", name=button)))


def open_detail(s: Surface, member: str = "12345"):
    """Inquire, then click the member's name link (located by table row/column)."""
    inquire(s, member)
    return s.click(spec(MAIN, TableCell(row_key=member, column_header="NAME")))
