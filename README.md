# Computer-Use Capabilities

An LLM discovers how to complete a goal in a UI that has no API. The successful run is saved as a
typed, versioned capability artifact, and that artifact is replayed deterministically with no LLM
in the loop. The project also includes human-in-the-loop handoff and safety guardrails. See
[CLAUDE.md](CLAUDE.md) for the project rules.

> Status: target app, surface layer, guardrails, the capability artifact schema, LLM discovery,
> deterministic replay and human-in-the-loop handoff are built.

## Install

```bash
uv sync
uv run playwright install chromium
cp .env.example .env   # fake demo creds; AWS creds come from your AWS profile
```

## Run the target app (Meridian Core mock bank)

```bash
uv run python -m target_app --port 8001 --tenant a   # or --tenant b
# open http://127.0.0.1:8001/  — sign on as demo_operator / demo-pass-123 (fake)
```

Seeded members (all fake): `12345 22222 31415 40300 55555 70001`. Member `40300` is restricted.

Fault injection (counter-based and deterministic; no auth needed):

```bash
curl -X POST localhost:8001/__faults -H 'content-type: application/json' \
     -d '{"latency_ms": 3000, "latency_count": 2}'
curl -X POST localhost:8001/__faults -d '{"interstitial_count": 1}' -H 'content-type: application/json'
curl -X POST localhost:8001/__faults -d '{"js_confirm_count": 1}' -H 'content-type: application/json'
curl -X POST localhost:8001/__faults -d '{"expire_session_now": true}' -H 'content-type: application/json'
curl -X POST localhost:8001/__faults -d '{"http500_count": 1, "http500_path_prefix": "/member"}' -H 'content-type: application/json'
curl localhost:8001/__faults            # current state
curl -X POST localhost:8001/__faults/reset
```

## Test

```bash
uv run pytest                          # unit/behavior tests (FastAPI TestClient)
uv run python scripts/smoke_target.py  # real headless browser drives the app end to end
uv run python scripts/observe.py       # print the Observation an LLM would see (member 12345)
uv run ruff check .
```

## Discover a capability (LLM)

Needs AWS credentials in the `AWS_PROFILE` profile and `BEDROCK_MODEL_ID` in `.env`. The target
app starts in the background automatically; the agent signs on with the profile's secrets, which
it never sees.

```bash
uv run cua discover \
  --goal "Look up member 12345 and read their Share Savings balance" \
  --id meridian.member.read_savings_balance --title "Read a member's share savings balance" \
  --input 'member_id=12345:string:^\d{5}$' --output savings_balance:money:money_to_decimal
# offline / no AWS: --llm scripted:<file.json>   (canned tool calls, see tests/discovery)
```

Each run writes `runs/<run_id>/` (events, redacted transcript, masked step screenshots,
summary.json) and a **draft** artifact in `capabilities/<id>/<version>.json`. Irreversible
actions ask for approval at the terminal (`--approve-mode deny` refuses them).

## Replay a capability (no LLM, no cloud credentials)

```bash
uv run cua replay meridian.member.read_savings_balance --input member_id=12345        # exit 0
uv run cua replay meridian.member.read_savings_balance --input member_id=99999        # exit 10
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --tenant b
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --repeat 5
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --json
# test harness only (POSTs /__faults, recorded in the run manifest):
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 \
  --inject-at s03_click_inquire '{"interstitial_count": 1}'
```

Exit codes: 0 success, 10 business outcome, 20 failed, 30 escalated, 2 invalid invocation.
Artifacts with irreversible steps replay only after `cua artifact approve`. Every run writes
`runs/<run_id>/result.json` (the typed `ReplayResult`), events and masked step screenshots.

## Human-in-the-loop handoff

```bash
uv run cua operator                      # terminal 1: console on http://127.0.0.1:8700/
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --hitl \
  --inject-at s04_click_name '{"http500_count": 1, "http500_path_prefix": "/member"}'
uv run cua discover ... --hitl --approve-mode console   # escalations + approvals in the console
uv run cua interventions list --status open
```

With `--hitl` a failure pauses automation and opens an intervention. An operator clicks **Take
control** and works in the SAME browser (the headed window, or DevTools attached to the
localhost CDP port). Their clicks and edits are captured (redacted), and **Resume** hands
control back. Replay then verifies the page before continuing. One owner at a time is
enforced by a control lease that GuardedSurface checks before every automation action.

Security: the console binds 127.0.0.1 and has no authentication. The CDP port is full control
of the browser, localhost only. A production deployment needs SSO, per-operator authorization,
an audit trail and an authenticated, proxied screencast instead of raw CDP.

## Capability artifacts

A capability is a typed, versioned JSON artifact (`capabilities/<id>/<version>.json`). It holds the
contract (inputs, outputs, business outcomes), the replayable steps with multi-strategy locators,
the checkpoints, and the approval/provenance records. App-wide behaviour (login, interstitials,
dialogs, error screens, shared outcomes, PII labels) lives in the app profile
`configs/app_profiles/meridian-core.yaml`. Per-tenant UI differences live in overlays under
`configs/app_profiles/tenants/`.

```bash
F=tests/fixtures/capabilities/meridian.member.read_savings_balance/1.0.0.json
uv run cua artifact validate $F          # schema + lint (all problems at once)
uv run cua artifact show $F              # human review rendering (markdown)
uv run cua artifact tool $F              # what a calling agent sees (JSON Schema in/out)
uv run cua artifact approve $F --reviewer NAME [--note ...]   # approve gated steps, record hash
uv run cua artifact schema               # export schemas/*.schema.json
uv run cua profile validate configs/app_profiles/meridian-core.yaml
```

> **The two artifacts under `tests/fixtures/capabilities/` are HAND-WRITTEN TEST FIXTURES**
> (`meridian.member.read_savings_balance`, `meridian.subaccount.open`). They exist to test the
> schema and to develop replay. The real artifacts in `capabilities/` will come from LLM discovery.

## Layout

| Path | Responsibility |
|---|---|
| `target_app/` | Mock legacy core-banking console (hostile DOM, frames, faults, tenants) |
| `cua/surface/` | Perceive and act on a UI surface (observer, actions, locators) |
| `cua/policy/` | Allowlist, risk classification, redaction |
| `cua/artifact/` | Capability schema (pydantic), app profiles, (de)serialization |
| `cua/discovery/` | LLM agent loop and recorder |
| `cua/replay/` | Deterministic replay engine and result contract |
| `cua/hitl/` | Control state machine, intervention store, operator console |
| `cua/obs/` | Structured event logging, evidence capture |
| `cua/cli.py` | `cua` CLI entry point |
| `configs/` | `policy.yaml`, `app_profiles/` (profile YAML + `tenants/` overrides) |
| `capabilities/` | Saved artifacts (JSON) |
| `schemas/` | Exported JSON Schemas (`cua artifact schema`) |
| `evidence/` | Committed, redacted evidence ([index](evidence/README.md)); `replay/` is regenerated by `scripts/make_evidence.py`, and `scripts/scan_evidence.py` checks it all |
| `runs/` | Per-run output (gitignored) |
