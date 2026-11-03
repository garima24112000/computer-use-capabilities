# Computer-Use Capabilities

An LLM discovers how to complete a goal in a UI that has no API. The successful run is saved as a
typed, versioned capability artifact, and that artifact is replayed deterministically with no LLM
in the loop. The project also includes human-in-the-loop handoff and safety guardrails. See
[CLAUDE.md](CLAUDE.md) for the project rules.

> Status: scaffold and target app only. The agent, replay engine and other parts come in later
> steps.

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
| `configs/` | `policy.yaml`, `app_profiles/` |
| `capabilities/` | Saved artifacts (JSON) |
| `evidence/` | Committed, redacted demo evidence |
| `runs/` | Per-run output (gitignored) |
