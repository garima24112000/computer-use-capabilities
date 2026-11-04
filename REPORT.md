# REPORT

An LLM discovers how to complete a goal in a legacy UI with no API. The run is compiled into a typed, versioned **capability**, which replays deterministically with no model in the loop and returns a structured result. When automation can't safely proceed, a human takes over the same live session and hands control back.

## 1. Architecture

```
 goal + contract ─► Discovery (LLM, Bedrock) ─records─► Capability (JSON) ─loads─► Replay (no LLM) ◄─ id + inputs
                          │                                                          │
                          └────────────── HITL controller + evidence log ────────────┘
                                                      │
   GuardedSurface: allowlist · risk tiers · approvals · redaction · control lease
   Surface: observe · describe · resolve · click/fill/select/press · screenshot
   Playwright web (built) │ Desktop UIA/AX (design) │ Vision + OCR (design)
                                                      ▼
                          Meridian Core (hostile mock core-banking app)
```

- **Target: a deliberately hostile mock (Meridian Core).** It uses a frameset and nested tables, with no ids, ARIA or test ids, labels in adjacent cells, `javascript:` links and an image SUBMIT button. Business outcomes are HTTP-200 pages. Faults (latency, interstitial, confirm dialog, session expiry, HTTP 500) are counter-based, so every error demo is reproducible. A tenant switch (A/B) stands in for one vendor product configured two ways. A public site couldn't produce these errors on demand.
- **The Surface seam.** Discovery and replay never call Playwright directly. Perception is a frame-aware, screen-reader-style scan (role, accessible name, label, table context), which works without a clean DOM and maps to UIA/AX on desktop.
- **One guard.** `GuardedSurface` enforces policy, approvals, redaction and the HITL lease before every action, backed by a network request filter.
- **The model is needed only for discovery.** `LLMClient` uses Bedrock Converse (Claude Sonnet 4.6, a tool call forced each turn), with a scripted client for tests. A test enforces that replay never imports it, so replay needs no credentials.
- **Simple on purpose.** One process per run, sync Playwright, canonical JSON artifacts in git, SQLite for interventions. No queues or services.

## 2. Artifact schema

A capability is **a contract first and a step list second**. Agents read the contract, reviewers read the steps, and replay executes them (`cua/artifact/schema.py`, `schemas/`).

| Part | Contents |
|---|---|
| Identity | `id`, semver `version` (MAJOR = contract, MINOR = steps/outcomes, PATCH = locators), `status` draft/approved/deprecated |
| Contract | typed `inputs` (pattern, enum, bounds, sensitive), typed `outputs` (normalizer, extract pattern), `secrets` as references, business `outcomes`; exported as a tool definition |
| Steps | stable id, human `intent`, typed action (Navigate/Click/Fill/Select/Press/Extract, no JS), `risk`, approval flags, `expect` checkpoint, retry |
| Targets | frame + ordered strategies: RoleName → LabelAnchor → TableCell → TextMatch → CssPath |
| Success, review, provenance | final checkpoint; `reviewed_by` + `approved_content_hash`; run id, model, redacted goal, transcript hash (never the transcript) |

**Why it's shaped this way:**
- **A tiny template grammar** (`{{inputs.x}}`, `{{secret:NAME}}`, no expressions), so artifacts are data, never code. The only templated locator is `TableCell.row_key = {{inputs.member_id}}`: the result row is keyed by the declared input, never by the member's name (PII), and never by "first row".
- **Approval is bound to the content hash.** Any later functional edit voids it, and replay then refuses an artifact with irreversible steps.
- **Lint** reports every problem at once: undeclared inputs, outputs not extracted exactly once, irreversible steps without approval, sensitive values in locators, and CSS-only extracts in discovered artifacts.
- **App profile**, one per vendor product: login sub-flow, interstitials, dialogs, session expiry, error signatures, scoped business outcomes and sensitive labels. Shared knowledge lives once, not in every capability.

## 3. Determinism & error handling

**Determinism:** no model, the same strategies in the same order, bounded waits, and a checkpoint after every step. A strategy is accepted **only if it matches exactly one element**. Zero or several matches fall through to the next strategy, and when nothing matches the failure lists every attempt. Uniqueness is also validated at record time. In the evidence, `--repeat 5` gives 5/5 successes with identical steps and outputs.

**After each step, a fixed order:**
1. An unknown dialog is a hard failure.
2. A known interstitial is recovered, then the sweep runs again.
3. Session expiry before a commit means re-login and restart from step 1. After a commit, it's `session_expired_after_commit` for a human to resolve.
4. App error pages are hard failures.
5. Business outcomes are returned as results.
6. The step's checkpoint is verified.

A missing target is first explained by known states: a missing NAME link usually means NO RECORDS FOUND.

| Class | Examples | Result / exit |
|---|---|---|
| Business outcome | not found, access denied, deposit out of range | `business_outcome` + code / 10 |
| Recoverable | interstitial, known dialog, re-login before commit, slow page | `success` + `recoveries[]` / 0 |
| Hard failure | HTTP 500 page, target not found, checkpoint mismatch, unknown dialog | `failed` + category, step, expected vs observed, screenshot / 20 |
| Caller error | bad input, invalid or unapproved artifact | `failed` before any browser starts / 20 |

There are 16 failure categories, each with a fixed `retryable` flag. Irreversible steps run **at most once**, and a settle retry re-waits but never re-clicks.

**Drift.** A lower-ranked winning strategy is reported as `degraded`, an early drift signal. If **only** the CSS fallback matches, a safe step proceeds with a flag, but a risky step is refused (`structural_only_on_risky`). The evidence shows this on tenant B without an override.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The recorded flow speaks in roles, names, labels, table positions and frames, and the seam is the `Surface` interface.
- **Legacy web** is the built case.
- **Desktop** would be a UIA/AX Surface. RoleName and LabelAnchor carry over, and CssPath becomes an automation-id or tree path.
- **No-tree surfaces** (Citrix, terminals) would use a vision Surface with text-anchor strategies.

Artifacts, replay, policy, redaction and HITL don't change.

**Multi-tenant reuse** (one of the two stretch goals built). Configuration layers as **vendor profile → version range → tenant override → capability**, instead of re-recording per tenant. An override may change only presentation (locators, labels, texts, timeouts), never risk, approvals, ids or the contract, so approval stays tied to the base artifact. In the evidence, the artifact recorded on tenant A replays on tenant B, which has different labels, branding and an extra column, through `configs/app_profiles/tenants/tenant-b.yaml`.

**Drift management.** Degraded matches per tenant and version trigger review before anything breaks. Structural-only matches on risky steps fail closed. The fix is an override or a PATCH/MINOR version, never a fork.

## 5. Escalation & handoff

**Detecting stuck:**
- **Discovery:** step budget exhausted, 3 invalid tool calls, 3 actions with no page change, the model calls `escalate`, an approval is denied, or policy blocks an action.
- **Replay:** a hard failure, a structural-only risky step, or an uncertain commit.

**Routing.** An intervention goes into SQLite (compare-and-set claims, so only one operator wins). It carries the capability, step and intent, reason, expected vs observed, a masked screenshot and how to reach the live session. A minimal console (`cua operator`) offers Take control, Resume, Mark handled, Abort, Approve and Deny.

**Control transfer:**
- A state machine: `AUTOMATION → AWAITING_HUMAN → HUMAN → RESUMING → AUTOMATION`, or `DONE`.
- A **lease** with one owner and an epoch counter. `GuardedSurface` refuses every automation action unless automation holds it.
- The human works in the **same BrowserContext** (the headed window, or a localhost CDP port). Tests assert the same context, page and session cookie throughout.
- The human's actions are captured, redacted and risk-flagged, but never replayed.

**Resume re-verifies before acting:**
1. Success checkpoint holds → extract the outputs.
2. The failed step's checkpoint holds → continue.
3. The target resolves and the step isn't irreversible → retry once.
4. Otherwise → re-escalate (at most 2 rounds).

The result records who intervened and which rule applied. A live run is in `evidence/hitl/` (video, run, intervention export).

**Minimal by design:** the console has no authentication and binds to localhost. Production would need SSO, audit and an authenticated screencast.

## 6. Safety

- **Allowlist** of origins, paths and action types. Blocked beats allowed, so the agent can't reach `/__faults`. A network filter fails closed.
- **Risk tiers:** reads and fills are safe, clicks are risky by default, and commits are irreversible (explicit rules plus a commit-verb regex). Each check returns ALLOW / REQUIRE_APPROVAL / BLOCK.
  - The live element is checked, and the stricter of live and recorded wins.
  - Pressing Enter inherits the risk of the form's submit control.
- **Irreversible actions:** discovery requires a human approval. Replay requires an approved artifact with a matching hash. They're never retried.
- **Data:**
  - Login is deterministic, so the LLM never sees credentials, and secrets are references.
  - Redaction is layered (structural by label, regex, exact values, values learned during the run). It covers every log write and everything sent to the model.
  - Locator strategies containing sensitive values are dropped, so PII can't enter an artifact.
  - Screenshot masks fail closed, and traces are never committed.
  - `scripts/scan_evidence.py` and a test scan `evidence/` and `capabilities/`.

**Limits:**
- Regex can miss free-text PII.
- The model sees non-sensitive page text (mitigated here by synthetic data; in deployment, discovery would run through Bedrock in the institution's own AWS account).
- Policy runs in-process, not in a sandbox.
- The scanner can't read text in images. The operator ID in pre-fix discovery screenshots was masked after the fact, and this is documented.
- The console has no authentication, and CDP gives full browser control (localhost only).
- Human actions are flagged, not blocked.
- The development IAM user uses the broad AmazonBedrockFullAccess policy; production would scope `bedrock:InvokeModel` to one model ARN.

## 7. Cuts

| Cut | Next |
|---|---|
| Co-browsing console, auth, audit | Authenticated CDP screencast, SSO, audit trail |
| Desktop and vision Surfaces | A UIA/AX Surface behind the same interface |
| Override registry, drift dashboards | Versioned profile/override store, per-tenant drift alerts |
| Teaching by demonstration | Compile captured human actions into a reviewed artifact patch |
| Assisted fallback | A bounded, policy-checked single-step LLM recovery, recorded and approval-gated |
| Services, queues, code generation | Not needed to prove the design |
| Known issue: one non-reproducible failure of `test_commit_uncertain_is_marked_handled_with_a_note` (nested escalation) | Errors inside escalation now abort cleanly as `escalation_error`; root-cause if it recurs |

**Stretch goals:** two were chosen, cross-tenant reuse (§4) and an agent-facing catalog (`cua catalog list` / `invoke`: capabilities as typed tools callable by name). `--repeat N` stability is a small extra on top of replay.
