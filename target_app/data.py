"""Seed data: obviously fake members (900-range SSNs, 555 phones, example.com emails)."""

import copy
from typing import Any

ACCOUNT_TYPES: dict[str, dict[str, str]] = {
    "SV": {"description": "Share Savings", "rate": "0.25%"},
    "MM": {"description": "Money Market", "rate": "1.10%"},
    "C12": {"description": "Certificate 12mo", "rate": "4.00%"},
}

RESTRICTED_MEMBERS = {"40300"}


def _acct(suffix: str, description: str, cents: int, rate: str) -> dict[str, Any]:
    return {"suffix": suffix, "description": description, "balance_cents": cents, "rate": rate}


SEED_MEMBERS: dict[str, dict[str, Any]] = {
    "12345": {
        "name": "SAMPLE, JOHN Q",
        "branch": "001 - MAIN OFFICE",
        "ssn": "900-12-3456",
        "address": "100 TEST LANE, SPRINGFIELD, ZZ 00001",
        "phone": "(555) 555-0101",
        "email": "john.sample@example.com",
        "accounts": [
            _acct("00", "Share Savings", 1234567, "0.25%"),
            _acct("10", "Checking", 250000, "0.05%"),
        ],
    },
    "22222": {
        "name": "EXAMPLE, MARIA T",
        "branch": "002 - NORTHGATE",
        "ssn": "900-22-2222",
        "address": "22 PLACEHOLDER AVE, SHELBYVILLE, ZZ 00002",
        "phone": "(555) 555-0122",
        "email": "maria.example@example.com",
        "accounts": [
            _acct("00", "Share Savings", 50000, "0.25%"),
            _acct("10", "Checking", 1298744, "0.05%"),
        ],
    },
    "31415": {
        "name": "SQUARED, PI R",
        "branch": "001 - MAIN OFFICE",
        "ssn": "900-31-4159",
        "address": "314 CIRCLE CT, SPRINGFIELD, ZZ 00001",
        "phone": "(555) 555-0314",
        "email": "pi.squared@example.com",
        "accounts": [
            _acct("00", "Share Savings", 314159, "0.25%"),
            _acct("10", "Checking", 2718, "0.05%"),
        ],
    },
    "40300": {
        "name": "RESTRICTED, RITA R",
        "branch": "003 - EASTSIDE",
        "ssn": "900-40-3000",
        "address": "403 FORBIDDEN WAY, CAPITAL CITY, ZZ 00003",
        "phone": "(555) 555-0403",
        "email": "rita.restricted@example.com",
        "accounts": [
            _acct("00", "Share Savings", 9999999, "0.25%"),
            _acct("10", "Checking", 100000, "0.05%"),
        ],
    },
    "55555": {
        "name": "FIVES, FIONA E",
        "branch": "002 - NORTHGATE",
        "ssn": "900-55-5555",
        "address": "5 FIVE POINTS RD, OGDENVILLE, ZZ 00005",
        "phone": "(555) 555-0555",
        "email": "fiona.fives@example.com",
        "accounts": [
            _acct("00", "Share Savings", 555555, "0.25%"),
            _acct("10", "Checking", 55500, "0.05%"),
        ],
    },
    "70001": {
        "name": "PLACEHOLDER, ZED A",
        "branch": "001 - MAIN OFFICE",
        "ssn": "900-70-0001",
        "address": "7 DEMO STREET APT 1, NORTH HAVERBROOK, ZZ 00007",
        "phone": "(555) 555-0701",
        "email": "zed.placeholder@example.com",
        "accounts": [
            _acct("00", "Share Savings", 2500, "0.25%"),
            _acct("10", "Checking", 0, "0.05%"),
        ],
    },
}


def fresh_members() -> dict[str, dict[str, Any]]:
    """Per-app mutable copy of the seed so opened sub-accounts never leak between instances."""
    return copy.deepcopy(SEED_MEMBERS)
