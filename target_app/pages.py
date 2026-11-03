"""HTML rendering for Meridian Core: 2005-era table soup with no ids, test ids or aria attributes."""

import json
from html import escape
from typing import Any

Tenant = dict[str, Any]

BUTTON_STYLE = (
    "background:#D4D0C8;border:2px outset #FFFFFF;padding:2px 10px;font-weight:bold;"
    "font-size:11px;cursor:pointer;text-align:center;color:#000000"
)
INPUT_STYLE = "font-family:Courier New;font-size:12px;border:1px solid #7F9DB9"

SUBMIT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="22">'
    '<rect x="0.5" y="0.5" width="79" height="21" fill="#D4D0C8" stroke="#404040"/>'
    '<text x="40" y="15" font-family="Verdana" font-size="11" font-weight="bold" '
    'text-anchor="middle" fill="#000000">SUBMIT</text></svg>'
)


def e(value: object) -> str:
    return escape(str(value), quote=True)


def js_str(value: str) -> str:
    """A JS string literal safe to embed inside a double-quoted HTML attribute."""
    return e(json.dumps(value))


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def document(t: Tenant, title: str, body: str, head_extra: str = "") -> str:
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>{e(t["bank_name"])} - {e(title)}</title>{head_extra}</head>
<body bgcolor="{t["page_bg"]}" style="margin:4px;font-family:Verdana,Tahoma,Arial;font-size:11px">
{body}
</body></html>"""


def panel(t: Tenant, heading: str, inner: str) -> str:
    return f"""<table width="100%" border="0" cellpadding="0" cellspacing="0"><tr><td bgcolor="{t["banner_bg"]}">
<table width="100%" border="0" cellpadding="3" cellspacing="1">
<tr><td bgcolor="{t["header_bg"]}" style="color:{t["header_fg"]};font-weight:bold;font-size:11px">{e(heading)}</td></tr>
<tr><td bgcolor="#FFFFFF" style="font-size:11px">{inner}</td></tr>
</table></td></tr></table>"""


def error_line(message: str) -> str:
    return (
        '<table border="0" cellpadding="4" cellspacing="0"><tr><td>'
        f'<font color="#CC0000"><b>{e(message)}</b></font></td></tr></table>'
    )


def notice_line(message: str) -> str:
    return (
        '<table border="0" cellpadding="4" cellspacing="0"><tr><td>'
        f"<b>{e(message)}</b></td></tr></table>"
    )


# --- sign on / frames ---------------------------------------------------------------------------


def login(t: Tenant, error: str | None = None) -> str:
    err = f'<tr><td colspan="2" align="center">{error_line(error)}</td></tr>' if error else ""
    body = f"""<br><br><br><center>
<form method="post" action="/login" name="frmA" style="margin:0">
<table border="1" cellpadding="0" cellspacing="0" width="380" bgcolor="#FFFFFF"><tr><td>
 <table width="100%" border="0" cellpadding="5" cellspacing="0">
  <tr><td colspan="2" bgcolor="{t["banner_bg"]}" style="color:{t["banner_fg"]}"><b>{e(t["bank_name"])}</b><br>
   <font size="1">{e(t["product"])}</font></td></tr>
  <tr><td colspan="2" bgcolor="{t["header_bg"]}" style="color:{t["header_fg"]}"><b>OPERATOR SIGN ON</b></td></tr>
  {err}
  <tr><td align="right" width="40%"><b>OPERATOR ID:</b></td>
      <td><input type="text" name="f1" size="18" maxlength="20" style="{INPUT_STYLE}"></td></tr>
  <tr><td align="right"><b>PASSWORD:</b></td>
      <td><input type="password" name="f2" size="18" maxlength="32" style="{INPUT_STYLE}"
           onkeypress="if(event.keyCode==13){{document.frmA.submit();}}"></td></tr>
  <tr><td>&nbsp;</td><td><div style="{BUTTON_STYLE};width:70px"
      onclick="document.frmA.submit()">SIGN ON</div></td></tr>
  <tr><td colspan="2"><font size="1" color="#808080">AUTHORIZED USE ONLY. ALL ACTIVITY IS LOGGED.</font></td></tr>
 </table>
</td></tr></table>
</form></center>"""
    return document(t, "SIGN ON", body)


def frameset(t: Tenant) -> str:
    return f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Frameset//EN">
<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>{e(t["bank_name"])} - {e(t["product"])}</title></head>
<frameset rows="64,*" border="1" frameborder="1">
  <frame src="/top" name="top" scrolling="no" noresize>
  <frameset cols="190,*">
    <frame src="/nav" name="nav" scrolling="auto">
    <frame src="/welcome" name="main" scrolling="auto">
  </frameset>
  <noframes><body>THIS APPLICATION REQUIRES A FRAMES-CAPABLE BROWSER.</body></noframes>
</frameset></html>"""


def top(t: Tenant, operator: str) -> str:
    body = f"""<table width="100%" border="0" cellpadding="4" cellspacing="0" bgcolor="{t["banner_bg"]}">
<tr><td style="color:{t["banner_fg"]}"><font size="3"><b>{e(t["bank_name"])}</b></font><br>
<font size="1">{e(t["product"])}</font></td>
<td align="right" style="color:{t["banner_fg"]};font-size:11px">OPERATOR: <b>{e(operator.upper())}</b>
&nbsp;|&nbsp;<a href="/logout" target="_top" style="color:{t["banner_fg"]}">SIGN OFF</a></td></tr></table>"""
    return document(t, "BANNER", body)


def nav(t: Tenant) -> str:
    cell = 'style="padding:4px;border-bottom:1px solid #808080;font-size:11px;font-weight:bold"'
    items = {
        "inquiry": (
            f'<tr><td {cell} onclick="parent.main.location.href=\'/inquiry\'" '
            f'onmouseover="this.style.background=\'#FFFFCC\'" '
            f'onmouseout="this.style.background=\'\'"><span style="cursor:pointer">'
            f"&raquo; MEMBER INQUIRY</span></td></tr>"
        ),
        "open": (
            f'<tr><td {cell}><a href="/subacct/open" target="main" '
            f'style="color:#000080;text-decoration:none">&raquo; OPEN SUB-ACCOUNT</a></td></tr>'
        ),
        "reports": (
            f'<tr><td {cell}><font color="#909090">&raquo; REPORTS</font> '
            f'<font size="1" color="#909090">(N/A)</font></td></tr>'
        ),
    }
    rows = "\n".join(items[key] for key in t["nav_order"])
    body = f"""<table width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="{t["nav_bg"]}">
<tr><td bgcolor="{t["header_bg"]}" style="color:{t["header_fg"]};padding:4px"><b>FUNCTIONS</b></td></tr>
{rows}
</table>"""
    return document(t, "MENU", body)


def welcome(t: Tenant) -> str:
    return document(t, "MAIN", panel(t, "WELCOME", "SELECT A FUNCTION FROM THE MENU AT LEFT."))


def logged_out(t: Tenant) -> str:
    body = notice_line("OPERATOR SIGNED OFF.") + '<a href="/login" target="_top">SIGN ON</a>'
    return document(t, "SIGNED OFF", body)


# --- system states ------------------------------------------------------------------------------


def session_expired(t: Tenant) -> str:
    body = (
        error_line("SESSION EXPIRED — PLEASE SIGN ON AGAIN")
        + '<table border="0" cellpadding="4"><tr><td>'
        '<a href="/login" target="_top">RETURN TO SIGN ON</a></td></tr></table>'
    )
    return document(t, "SESSION EXPIRED", body)


def system_error(t: Tenant) -> str:
    body = """<table border="2" cellpadding="8" cellspacing="0" bgcolor="#FFFFFF"><tr><td>
<font color="#CC0000" size="3"><b>SYSTEM ERROR 0xC0FFEE — CONTACT ADMINISTRATOR</b></font><br>
<font size="1">MODULE: MCSRV32.DLL &nbsp; TRANSACTION ROLLED BACK.</font></td></tr></table>"""
    return document(t, "SYSTEM ERROR", body)


def interstitial(t: Tenant, target: str) -> str:
    body = panel(
        t,
        "SYSTEM NOTICE",
        "SYSTEM NOTICE — Scheduled maintenance tonight 11PM. "
        f'<a href="javascript:location.replace({js_str(target)})">[CONTINUE]</a>',
    )
    return document(t, "SYSTEM NOTICE", body)


# --- member inquiry -----------------------------------------------------------------------------


def inquiry(
    t: Tenant,
    value: str = "",
    error: str | None = None,
    notice: str | None = None,
    member: tuple[str, dict[str, Any]] | None = None,
) -> str:
    form = f"""<form method="get" action="/inquiry" name="frmQ" style="margin:0">
<table border="0" cellpadding="3" cellspacing="0"><tr>
<td align="right"><b>{e(t["member_label"])}</b></td>
<td><input type="text" name="txtA" size="8" maxlength="10" value="{e(value)}" style="{INPUT_STYLE}"></td>
<td><a href="javascript:document.frmQ.submit()" style="{BUTTON_STYLE};text-decoration:none">{e(t["inquire_label"])}</a></td>
</tr></table></form>"""
    result = ""
    if error:
        result = error_line(error)
    elif notice:
        result = notice_line(notice)
    elif member:
        number, m = member
        result = f"""<script type="text/javascript">function openMbr(n){{location.href='/member/detail?m='+n;}}</script>
<table border="1" cellpadding="3" cellspacing="0" bordercolor="#808080" width="70%">
<tr bgcolor="{t["header_bg"]}" style="color:{t["header_fg"]}"><td><b>{e(t["member_label"])}</b></td><td><b>NAME</b></td><td><b>BRANCH</b></td></tr>
<tr><td>{e(number)}</td><td><a href="javascript:openMbr('{e(number)}')">{e(m["name"])}</a></td><td>{e(m["branch"])}</td></tr>
</table>"""
    return document(t, "MEMBER INQUIRY", panel(t, "MEMBER INQUIRY", form + "<br>" + result))


# --- member detail ------------------------------------------------------------------------------


def member_detail(t: Tenant, number: str, m: dict[str, Any], js_confirm: bool) -> str:
    label = 'style="font-weight:bold;background:#EEEEEE;white-space:nowrap" width="25%"'
    info_rows = [
        ("MEMBER NAME", m["name"]),
        ("SSN", m["ssn"]),
        ("ADDRESS", m["address"]),
        ("PHONE", m["phone"]),
        ("EMAIL", m["email"]),
        ("BRANCH", m["branch"]),
    ]
    info = "\n".join(f"<tr><td {label}>{k}</td><td>{e(v)}</td></tr>" for k, v in info_rows)

    extra = t["accounts_extra_column"]
    extra_head = f"<td><b>{e(extra[0])}</b></td>" if extra else ""
    acct_rows = "\n".join(
        f"<tr><td>{e(a['suffix'])}</td><td>{e(a['description'])}</td>"
        f'<td align="right">{money(a["balance_cents"])}</td>'
        + (f"<td align='right'>{e(a[extra[1]])}</td>" if extra else "")
        + "</tr>"
        for a in m["accounts"]
    )
    accounts = f"""<table border="1" cellpadding="3" cellspacing="0" bordercolor="#808080" width="100%">
<tr bgcolor="{t["header_bg"]}" style="color:{t["header_fg"]}"><td><b>SFX</b></td><td><b>DESCRIPTION</b></td><td align="right"><b>BALANCE</b></td>{extra_head}</tr>
{acct_rows}
</table>"""

    actions = f"""<table border="0" cellpadding="2" cellspacing="4"><tr>
<td style="{BUTTON_STYLE}" onclick="location.href='/subacct/open?m={e(number)}'">OPEN SUB-ACCOUNT</td>
<td><a href="/inquiry">NEW INQUIRY</a></td></tr></table>"""

    inner = f"""<table width="100%" border="0" cellpadding="0" cellspacing="6"><tr><td valign="top">
<table width="100%" border="1" cellpadding="3" cellspacing="0" bordercolor="#C0C0C0">{info}</table>
</td></tr>
<tr><td valign="top">{panel(t, "ACCOUNTS", accounts)}</td></tr>
<tr><td>{actions}</td></tr></table>"""

    head = ""
    if js_confirm:
        head = (
            '<script type="text/javascript">'
            'if(!window.confirm("Unposted items exist. Continue?")){location.replace("/inquiry");}'
            "</script>"
        )
    return document(t, "MEMBER DETAIL", panel(t, f"MEMBER DETAIL - {number}", inner), head)


# --- open sub-account ---------------------------------------------------------------------------


def open_form(t: Tenant, values: dict[str, str], types: dict[str, dict[str, str]],
              error: str | None = None) -> str:
    options = "".join(
        f'<option value="{code}"{" selected" if values.get("type") == code else ""}>'
        f"{e(info['description'])}</option>"
        for code, info in types.items()
    )
    lbl = 'align="right" style="font-weight:bold;white-space:nowrap"'
    form = f"""{error_line(error) if error else ""}
<form method="post" action="/subacct/review" name="frmO" style="margin:0">
<table border="0" cellpadding="3" cellspacing="0">
<tr><td {lbl}>{e(t["member_label"])}</td><td><input type="text" name="txtB" size="8" maxlength="10" value="{e(values.get("member", ""))}" style="{INPUT_STYLE}"></td></tr>
<tr><td {lbl}>ACCOUNT TYPE</td><td><select name="selC" style="font-size:11px">{options}</select></td></tr>
<tr><td {lbl}>INITIAL DEPOSIT</td><td><input type="text" name="txtD" size="12" maxlength="14" value="{e(values.get("amount", ""))}" style="{INPUT_STYLE}"></td></tr>
<tr><td {lbl}>NICKNAME</td><td><input type="text" name="txtE" size="20" maxlength="20" value="{e(values.get("nickname", ""))}" style="{INPUT_STYLE}"></td></tr>
<tr><td>&nbsp;</td><td><table border="0" cellpadding="0" cellspacing="0"><tr>
<td style="{BUTTON_STYLE}" onclick="document.frmO.submit()">CONTINUE</td></tr></table></td></tr>
</table></form>"""
    return document(t, "OPEN SUB-ACCOUNT", panel(t, "OPEN SUB-ACCOUNT", form))


def review(t: Tenant, member: str, name: str, type_code: str, type_desc: str,
           cents: int, nickname: str) -> str:
    lbl = 'style="font-weight:bold;background:#EEEEEE"'
    body = f"""<form method="post" action="/subacct/submit" name="frmR" style="margin:0">
<input type="hidden" name="h1" value="{e(member)}"><input type="hidden" name="h2" value="{e(type_code)}">
<input type="hidden" name="h3" value="{cents}"><input type="hidden" name="h4" value="{e(nickname)}">
<table border="1" cellpadding="3" cellspacing="0" bordercolor="#C0C0C0">
<tr><td {lbl}>{e(t["member_label"])}</td><td>{e(member)}</td></tr>
<tr><td {lbl}>MEMBER NAME</td><td>{e(name)}</td></tr>
<tr><td {lbl}>ACCOUNT TYPE</td><td>{e(type_desc)}</td></tr>
<tr><td {lbl}>INITIAL DEPOSIT</td><td>{money(cents)}</td></tr>
<tr><td {lbl}>NICKNAME</td><td>{e(nickname) or "&nbsp;"}</td></tr>
</table><br>
<font size="1">REVIEW ENTRIES. SUBMIT IS FINAL AND CANNOT BE REVERSED.</font><br><br>
<table border="0" cellpadding="0" cellspacing="6"><tr>
<td><input type="image" name="imgS" src="/img/btn_submit.svg" width="80" height="22" border="0"></td>
<td><a href="javascript:location.replace({js_str(f"/member/detail?m={member}")})">CANCEL</a></td>
</tr></table></form>"""
    return document(t, "REVIEW", panel(t, "OPEN SUB-ACCOUNT - REVIEW", body))


def confirmation(t: Tenant, conf_no: str, member: str, suffix: str, type_desc: str,
                 cents: int) -> str:
    body = f"""<table border="0" cellpadding="4"><tr><td><font color="#006600" size="3"><b>SUB-ACCOUNT OPENED — CONFIRMATION NO. {e(conf_no)}</b></font></td></tr>
<tr><td>{e(t["member_label"])} {e(member)} &nbsp; SFX {e(suffix)} &nbsp; {e(type_desc)} &nbsp; {money(cents)}</td></tr>
<tr><td><a href="/member/detail?m={e(member)}">RETURN TO MEMBER</a></td></tr></table>"""
    return document(t, "CONFIRMATION", panel(t, "OPEN SUB-ACCOUNT - CONFIRMATION", body))
