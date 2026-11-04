"""Scan evidence/ and capabilities/ for anything that must never be committed.

Looks for:
  - the operator id and password (any case), from the environment / .env or the documented
    fake defaults;
  - every seeded fake PII value in target_app/data.py (names, SSNs, phones, emails, addresses);
  - SSN / card (Luhn-checked) / email / phone patterns;
  - AWS key material (AKIA.../ASIA... ids, aws_secret_access_key / aws_session_token);
  - the local username and home directory;
  - any file larger than 20 MB (except evidence/hitl/hitl_handoff.mp4).

Text files are matched with the regexes; binary files (png, mp4, ...) are searched for the
literal values only. Note: this cannot see text rendered INSIDE images; screenshots are masked
at capture time by GuardedSurface.

Exit 0 when clean, 1 with a list of hits. Usage: uv run python scripts/scan_evidence.py [dir ...]
"""

import getpass
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from target_app.data import SEED_MEMBERS  # noqa: E402

DEFAULT_DIRS = [ROOT / "evidence", ROOT / "capabilities"]
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".diff", ".yaml", ".yml", ".html", ".csv"}
MAX_BYTES = 20 * 1024 * 1024
SIZE_EXEMPT = {Path("evidence/hitl/hitl_handoff.mp4")}

PATTERNS = {
    "ssn": re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<![\w-])\(?\d{3}\)?[ .-]\d{3}-\d{4}(?![\w-])"),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws_secret": re.compile(r"aws_secret_access_key|aws_session_token", re.IGNORECASE),
}
CARD = re.compile(r"(?<![\w.])(?:\d[ -]?){12,18}\d(?![\w.])")


def _env_secrets() -> dict[str, str]:
    values = {"BANK_OPERATOR_USER": "demo_operator", "BANK_OPERATOR_PASS": "demo-pass-123"}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in values and value.strip():
                values[key.strip()] = value.strip().strip("'\"")
    for key in values:
        values[key] = os.environ.get(key) or values[key]
    return values


def literals() -> list[tuple[str, str]]:
    """(label, value) pairs matched case-insensitively."""
    out = [(f"secret {k}", v) for k, v in _env_secrets().items()]
    for member, m in SEED_MEMBERS.items():
        for field in ("name", "ssn", "address", "phone", "email"):
            out.append((f"seed PII {member}.{field}", m[field]))
        out.append((f"seed PII {member}.phone (bare)", m["phone"].split(") ")[-1]))
    return out


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def scan_file(path: Path, rel: Path, lits: list[tuple[str, str]], user: str, home: str
              ) -> list[str]:
    hits = []
    size = path.stat().st_size
    if size > MAX_BYTES and rel not in SIZE_EXEMPT:
        hits.append(f"{rel}: file is {size / 1e6:.1f} MB (> 20 MB)")
    data = path.read_bytes()
    lower = data.lower()
    for label, value in lits:
        if value.lower().encode() in lower:
            hits.append(f"{rel}: {label}")
    for needle in {home, f"/Users/{user}", f"/home/{user}"}:
        if needle.encode() in data:
            hits.append(f"{rel}: local home path {needle!r}")
    if path.suffix not in TEXT_SUFFIXES:
        return hits
    text = data.decode("utf-8", errors="replace")
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", text, re.IGNORECASE):
        hits.append(f"{rel}: local username {user!r}")
    for label, rx in PATTERNS.items():
        for m in rx.finditer(text):
            hits.append(f"{rel}: {label} pattern {m.group(0)!r}")
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            hits.append(f"{rel}: card-like number {m.group(0)!r}")
    return hits


def scan(dirs: list[Path]) -> list[str]:
    lits, user, home = literals(), getpass.getuser(), str(Path.home())
    hits: list[str] = []
    for d in dirs:
        for path in sorted(p for p in d.rglob("*") if p.is_file()):
            rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            hits += scan_file(path, rel, lits, user, home)
    return hits


def main(argv: list[str]) -> int:
    dirs = [Path(a).resolve() for a in argv] or [d for d in DEFAULT_DIRS if d.exists()]
    files = sum(1 for d in dirs for p in d.rglob("*") if p.is_file())
    hits = scan(dirs)
    for h in hits:
        print(f"HIT {h}")
    shown = ", ".join(str(d.relative_to(ROOT)) if d.is_relative_to(ROOT) else str(d)
                      for d in dirs)
    print(f"scanned {files} files in {shown}: "
          + ("clean" if not hits else f"{len(hits)} hit(s)"))
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
