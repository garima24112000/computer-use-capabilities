"""Tenant configuration: every difference between tenant deployments lives in TENANTS."""

from typing import Any

TENANTS: dict[str, dict[str, Any]] = {
    "a": {
        "bank_name": "LAKESHORE MUTUAL CREDIT UNION",
        "product": "MERIDIAN CORE v4.2.1 - SERVICING CONSOLE",
        "banner_bg": "#003366",
        "banner_fg": "#FFFFFF",
        "header_bg": "#6699CC",
        "header_fg": "#FFFFFF",
        "page_bg": "#E8E8E8",
        "nav_bg": "#C0C0C0",
        "member_label": "MEMBER NO.",
        "inquire_label": "INQUIRE",
        "nav_order": ["inquiry", "open", "reports"],
        "accounts_extra_column": None,
    },
    "b": {
        "bank_name": "PRAIRIE HARVEST FEDERAL CREDIT UNION",
        "product": "MERIDIAN CORE v4.2.1 - SERVICING CONSOLE",
        "banner_bg": "#5C1A1A",
        "banner_fg": "#FFF8DC",
        "header_bg": "#8B7D3A",
        "header_fg": "#FFFFFF",
        "page_bg": "#F5F0DC",
        "nav_bg": "#D8CFA8",
        "member_label": "ACCT HOLDER #",
        "inquire_label": "SEARCH",
        "nav_order": ["open", "inquiry", "reports"],
        # (column header, account field) appended to the ACCOUNTS table.
        "accounts_extra_column": ("RATE", "rate"),
    },
}
