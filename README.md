# Computer-Use Capabilities

An LLM **discovers** how to complete a goal in a legacy UI that has no API. The successful run is
compiled into a typed, versioned **capability artifact** (a contract plus reviewable steps), and
the artifact is **replayed deterministically with no LLM in the loop**. Replay returns a structured
result that separates business outcomes, recoveries and hard failures. When automation can't
continue safely, a **human takes over the same live browser session** and hands control back.
Guardrails cover every action: an allowlist, risk tiers, approvals for irreversible steps,
redaction, and a control lease.

The target is **Meridian Core**, a deliberately hostile mock core-banking console: framesets,
nested tables, no ids/ARIA/labels, `javascript:` links, and counter-based fault injection.

- Design write-up: [REPORT.md](REPORT.md)
- Evidence index (discovery, replay, HITL): [evidence/README.md](evidence/README.md)
- HITL handoff video: [evidence/hitl/hitl_handoff.mp4](evidence/hitl/hitl_handoff.mp4)

**Live human handoff demo (3 min, voiceover):**

  

https://github.com/user-attachments/assets/ece32f34-b56d-4d1c-beae-c606183e6870



## Repository layout

```
cua/                 the system (Python package)
  surface/           Surface interface + Playwright web implementation (frame-aware DOM scan, locators)
  policy/            GuardedSurface, allowlist/risk rules, approvals, redaction, network filter
  artifact/          capability schema, lint, app profiles, tenant overrides, store, renderers
  discovery/         LLM loop (LLMClient: Bedrock Converse + scripted), tools, prompts, recorder
  execute.py         deterministic action/checkpoint executor shared by discovery and replay
  replay/            replay engine, result contract, session wiring, test-only fault harness
  hitl/              control state machine + lease, intervention store, capture, operator console
  obs/               run directories, redacted event log, transcript, evidence capture
  catalog.py         capabilities as agent tools (`cua catalog`)
  cli.py             the `cua` command
target_app/          Meridian Core mock (FastAPI): tenants, seed data, fault injection
configs/             policy.yaml, app_profiles/ (meridian-core.yaml + tenants/ overrides)
capabilities/        saved artifacts: capabilities/<id>/<version>.json
schemas/             exported JSON Schemas (`cua artifact schema`)
scripts/             make_evidence.py, scan_evidence.py, smoke/observe helpers
evidence/            committed, redacted evidence (see its README)
tests/               pytest suite (scripted LLM, local app, real headless Chromium)
runs/                per-run output (gitignored)
```

## Setup

Prerequisites: macOS or Linux, and [uv](https://docs.astral.sh/uv/). uv installs Python 3.11.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

`.env` holds the fake operator sign-on for the mock bank (`BANK_OPERATOR_USER`,
`BANK_OPERATOR_PASS`). It is gitignored and never holds AWS keys.

**Discovery only** needs AWS Bedrock:
- `AWS_PROFILE`, `AWS_REGION` and `BEDROCK_MODEL_ID` (an inference profile id) set in `.env`.
- Credentials come from that AWS profile, never from `.env`.
- Model access enabled in the Bedrock console.

**Replay, the tests, the operator console and the evidence scripts need NO cloud credentials and
no network.** Everything runs against the local mock app on 127.0.0.1.

## Run without live services

```bash
uv run pytest                 # full suite: scripted LLM, local app, headless Chromium (~9 min)
uv run ruff check .

# Discovery with canned tool calls instead of Bedrock (writes to runs/, which is gitignored)
uv run cua discover \
  --goal "Look up member 12345 and read their Share Savings balance" \
  --id meridian.member.read_savings_balance --title "Read a member's share savings balance" \
  --input 'member_id=12345:string:^\d{5}$' --output savings_balance:money:money_to_decimal \
  --llm scripted:tests/fixtures/scripted/read_savings_balance.json \
  --runs-dir runs --capabilities-dir runs/scripted-capabilities

# Replay the committed artifacts (the target app starts in the background on a free port)
uv run cua replay meridian.member.read_savings_balance --input member_id=12345
```

To look at the mock app yourself: `uv run python -m target_app --port 8001 --tenant a` (or
`--tenant b`), then open http://127.0.0.1:8001/ and sign on as `demo_operator` /
`demo-pass-123` (fake). The seeded members are `12345 22222 31415 40300 55555 70001`; `40300` is
restricted.

## Demo path

### a. Discovery (real LLM)

These are the exact commands used for the committed 1.1.0 artifacts:

```bash
uv run cua discover \
  --goal "Look up member 12345 and read their Share Savings balance" \
  --id meridian.member.read_savings_balance \
  --title "Read a member's share savings balance" \
  --description "Looks up a member by member number and returns the balance of the base Share Savings account." \
  --input 'member_id=12345:string:^\d{5}$' \
  --input-desc member_id="5-digit member number" \
  --output savings_balance:money:money_to_decimal \
  --headed

uv run cua discover \
  --goal "Open a new Money Market sub-account for member 22222 with an initial deposit of 250.00 and nickname Rainy Day, and reach the confirmation screen" \
  --id meridian.subaccount.open \
  --title "Open a sub-account for a member" \
  --description "Opens a new sub-account with an initial deposit and returns the core's confirmation number. The final SUBMIT is irreversible and requires approval." \
  --input 'member_id=22222:string:^\d{5}$' \
  --input 'account_type=Money Market:enum:Share Savings|Money Market|Certificate 12mo' \
  --input 'initial_deposit=250.00:decimal:0.01..10000' \
  --input 'nickname?=Rainy Day:string:^[A-Za-z0-9 ]{0,20}$' \
  --input-desc member_id="5-digit member number" \
  --input-desc initial_deposit="Initial deposit in dollars" \
  --output 'confirmation_number:string:strip:(MC-\d{6})' \
  --headed
```

- A new discovery saves the **next MINOR version** as a draft. With 1.1.0 committed, that is
  `1.2.0`.
- The sub-account run stops at the irreversible SUBMIT and asks `y/N` at the terminal
  (`--approve-mode prompt`, the default).
- Each run writes `runs/<run_id>/`: events, the redacted transcript, masked screenshots,
  `summary.json`, and `manifest.json` with the CLI argv.

### b. Review and approve

```bash
uv run cua artifact show capabilities/meridian.subaccount.open/1.1.0.json      # review rendering
uv run cua artifact tool capabilities/meridian.subaccount.open/1.1.0.json      # agent's view
uv run cua artifact approve capabilities/meridian.subaccount.open/1.1.0.json --reviewer NAME --note "..."
```

Approval records the content hash. Any later functional edit voids it, and replay then refuses
the irreversible steps.

### c. Replay (no LLM)

Each command below is followed by the first line of its result:

```bash
uv run cua replay meridian.member.read_savings_balance --input member_id=12345
# SUCCESS  meridian.member.read_savings_balance v1.1.0  (approved)  ... ms  run ...   (exit 0)

uv run cua replay meridian.member.read_savings_balance --input member_id=99999
# BUSINESS_OUTCOME  meridian.member.read_savings_balance v1.1.0  (approved) ...        (exit 10)

# Fault injection is a TEST HARNESS (POST /__faults), recorded in the run manifest
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 \
  --inject-at s03_click_inquire '{"interstitial_count": 1}'
# SUCCESS ...   then: recovery interstitial:maintenance_notice at s03_click_inquire

uv run cua replay meridian.member.read_savings_balance --input member_id=12345 \
  --inject-at s04_click_name '{"http500_count": 1, "http500_path_prefix": "/member"}'
# FAILED ...    then: failure app_error at s04_click_name (retryable=true)       (exit 20)

uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --tenant b
# SUCCESS ...   (tenant B's UI drift handled by configs/app_profiles/tenants/tenant-b.yaml)

uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --tenant b --no-override
# FAILED ...    then: failure structural_only_on_risky at s03_click_inquire (retryable=false)

uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --repeat 5
# SUCCESS ...   then: STABILITY over 5 runs: success=5, identical step sequence: yes

uv run cua replay meridian.subaccount.open --input member_id=31415 \
  --input "account_type=Share Savings" --input initial_deposit=100.00
# SUCCESS  meridian.subaccount.open v1.1.0  (approved) ...  then: confirmation_number = MC-......
```

Add `--json` to print the full `ReplayResult`. Every run writes `runs/<run_id>/result.json`.

### d. HITL live handoff

```bash
uv run cua operator        # terminal 1: console on http://127.0.0.1:8700/
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --hitl \
  --inject-at s04_click_name '{"http500_count": 1, "http500_path_prefix": "/member"}'   # terminal 2
```

1. Terminal 2 opens a Chromium window. Replay hits SYSTEM ERROR 0xC0FFEE and waits; the banner
   in the browser reads "WAITING FOR AN OPERATOR".
2. In the console, open the `replay_failure` intervention, enter your name and click
   **Take control**. The banner changes to "HUMAN IN CONTROL".
3. In the browser window, do the lookup by hand: **» MEMBER INQUIRY**, type `12345`, click
   **INQUIRE**, then click the member's name. The console lists your actions, redacted.
4. Click **Resume** in the console. Replay re-verifies the page and prints `SUCCESS`,
   `savings_balance = 12345.67`, and a `human_intervention` recovery (rule
   `success_checkpoint`).

`uv run cua interventions list` shows the intervention as resolved. Discovery supports the same
flow with `--hitl --approve-mode console`.

### e. Capability catalog (capabilities as agent tools)

```bash
uv run cua catalog list                  # latest approved version of each capability (--all: drafts too)
uv run cua catalog list --json           # the tool definitions: input/output JSON Schema + outcome codes
uv run cua catalog invoke meridian_member_read_savings_balance --args '{"member_id": "12345"}'
```

`invoke` validates the arguments against the tool's `input_schema` and the contract. Invalid
arguments exit 2 before any browser starts. It then runs the normal replay path, with the same
exit codes.

### f. Regenerate and check the evidence

```bash
uv run python scripts/make_evidence.py            # all replay scenarios -> evidence/replay/
uv run python scripts/make_evidence.py success_12345   # or only the named scenarios
uv run python scripts/scan_evidence.py            # secrets / PII / AWS keys / local paths / size
```

## Result contract and exit codes

Every replay returns a typed `ReplayResult` (`cua/replay/result.py`): status, outputs or
business outcome, and on failure a `failure` with category, step id, expected vs observed,
retryable flag and evidence paths. It also lists `recoveries`, per-step `drift` and
`irreversible_executed`.

| Exit | Status | Meaning |
|---|---|---|
| 0 | `success` | Outputs extracted. Any recoveries are listed. |
| 10 | `business_outcome` | The app answered with a known business state (e.g. MEMBER_NOT_FOUND). This is not an error. |
| 20 | `failed` | A typed failure category. The table in `cua/replay/result.py` fixes each category's `retryable` flag. |
| 30 | `escalated` | Handed to a human, who resolved it outside automation (e.g. "Mark handled"). |
| 2 | - | Invalid invocation (bad flags, unknown artifact or tool, invalid catalog args). Nothing ran. |

## Where things are

| Brief | Code | Evidence |
|---|---|---|
| 3.1 Goal-driven agent loop | `cua/discovery/` (agent, tools, prompts, recorder), `cua/surface/` | `evidence/discovery/` |
| 3.2 Structured artifact (agent-invocable capability) | `cua/artifact/`, `capabilities/`, `schemas/`, `cua/catalog.py` | `evidence/discovery/*/artifact_show.md`, `evidence/catalog/tools.json`, `evidence/replay/catalog_invoke_read_balance/` |
| 3.3 Deterministic replay | `cua/replay/`, `cua/execute.py` | `evidence/replay/` (18 scenarios) |
| 3.4 Safety & policy guardrails | `cua/policy/`, `configs/policy.yaml` | `evidence/replay/subaccount_*`, `evidence/replay/commit_uncertain_*` |
| 3.5 Evidence / observability | `cua/obs/`, `scripts/make_evidence.py`, `scripts/scan_evidence.py` | `evidence/README.md` |
| 3.6 Human-in-the-loop escalation & handoff | `cua/hitl/`, `cua/replay/engine.py` (resume rules) | `evidence/hitl/` |
| 3.7 Design for heterogeneity & scale (design in [REPORT.md §4](REPORT.md#4-heterogeneity--multi-tenant)) | `cua/surface/base.py`, `cua/artifact/overrides.py`, `configs/app_profiles/tenants/` | `evidence/replay/drift_tenant_b_*` |

## Security notes

- **Synthetic data only.** The mock bank's members, SSNs, phones and emails are fake, and so are
  the operator credentials. Nothing secret or PII is written to disk unredacted, and screenshots
  are masked.
- **Console and CDP are localhost only, with no authentication.** The operator console binds
  127.0.0.1, and the `--hitl` CDP port gives full control of the browser. Production needs SSO,
  authorization, an audit trail and an authenticated screencast.
- **Traces are unmasked.** Playwright traces (`--trace`, `runs/<id>/trace.zip`) contain the raw
  DOM. They are opt-in and must never be committed.

## AI-assisted development

This project was built with Claude Code; the design decisions, reviews and approvals are mine (see
[REPORT.md](REPORT.md)).
