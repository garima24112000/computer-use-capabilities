# REPORT

A computer-use system for legacy back-office apps that have no API. An LLM discovers how to complete a goal in a live UI. The successful run is compiled into a typed, versioned **capability artifact**, and that artifact is replayed deterministically, with no model in the loop. Replay returns a structured result that separates business outcomes, recoveries and hard failures. When automation can't safely proceed, a human takes over the same live browser session and hands control back.

## 1. Architecture

```
            goal + declared contract                     capability id + inputs
                      │                                            │
              ┌───────▼────────┐   records    ┌──────────┐  loads  ┌▼──────────────┐
              │ Discovery loop │─────────────►│ Artifact │────────►│ Replay engine │
              │ (LLM: Bedrock) │              │  (JSON)  │         │   (no LLM)    │
              └───────┬────────┘              └──────────┘         └──────┬────────┘
                      │          HITL controller + evidence log           │
              ┌───────▼───────────────────────────────────────────────────▼────────┐
              │ GuardedSurface: allowlist · risk tiers · approvals · redaction ·    │
              │                 control lease                                      │
              ├────────────────────────────────────────────────────────────────────┤
              │ Surface interface: observe · describe · resolve · act · screenshot  │
              ├──────────────────────┬──────────────────────┬──────────────────────┤
              │ Playwright web (built)│ Desktop UIA/AX (design)│ Vision + OCR (design) │
              └──────────┬───────────┴──────────────────────┴──────────────────────┘
                         ▼
              Meridian Core (mock legacy core-banking app)
```

**Key decisions**

- **Target: a deliberately hostile mock app (Meridian Core)**, built instead of using a public site. It uses a `<frameset>` and nested tables, and has no ids, ARIA attributes or test ids. Labels sit in adjacent `<td>`s, links use `javascript:` hrefs, and SUBMIT is an image button. Its business outcomes are in-page, HTTP-200 states. Faults are **counter-based** and injected through `/__faults`: latency, a maintenance interstitial, a native confirm dialog, session expiry and HTTP 500. That makes every error demo reproducible. A tenant switch (A/B) stands in for one vendor product configured two ways.
- **The Surface seam.** Discovery and replay talk only to a `Surface` interface, never to Playwright. Perception is a frame-aware, accessibility-style scan. It computes role, accessible name, nearby label and table context the way a screen reader would, because native ARIA names are empty on this markup. The same idea exists on desktop as UIA (Windows) and AX (macOS).
- **One guard for everything.** `GuardedSurface` wraps any Surface and enforces policy, approvals, redaction and the HITL control lease before every action. A network-level request filter backs it up. Neither discovery nor replay can bypass it.
- **The model is a discovery-time dependency only.** `LLMClient` has a Bedrock Converse implementation (Claude Sonnet 4.6, `toolChoice: any`) and a scripted one for tests. Replay never imports it, and a subprocess test enforces this, so replay needs no cloud credentials.
- **Simple on purpose.** It's a single Python process per run, using the sync Playwright API. Artifacts are canonical JSON in git, and interventions live in SQLite so the console can be a separate process. There are no queues or services: the brief rewards good seams, not scaling infrastructure.

## 2. Artifact schema

A capability is **a contract first and a step list second**. A calling agent reads the contract, a reviewer reads the steps, and replay executes them (`cua/artifact/schema.py`, JSON Schema in `schemas/`).

| Part | Contents | Why |
|---|---|---|
| Identity | `id` (e.g. `meridian.member.read_savings_balance`), semver `version`, `status` draft/approved/deprecated, `schema_version` | MAJOR = contract change, MINOR = steps/outcomes, PATCH = locators/timeouts; agents pin MAJOR |
| Contract | typed `inputs` (pattern, enum, bounds, `sensitive`), typed `outputs` (normalizer, `extract_pattern`), `secrets` (references only), business `outcomes` | Exported as a tool definition (`cua artifact tool`) |
| Preconditions | entry path; session via the app profile's login sub-flow | Credentials never live in capabilities |
| Steps | stable `id`, human `intent`, typed action (`Navigate`, `Click`, `Fill`, `Select`, `Press`, `Extract`; no free-form JS), `risk`, `requires_approval`/`approved`, `expect` checkpoint, retry, settle timeout | Reviewable line by line; every step verifies where it landed |
| Targets | `TargetSpec` = frame + ordered strategies: RoleName → LabelAnchor → TableCell → TextMatch → CssPath | Semantic first, structural last (see §3) |
| Success | final checkpoint | Success is asserted, not assumed |
| Review | reviewer, `approved_at`, `approved_content_hash` | Approval is bound to the exact content |
| Provenance | source (discovery / hand_written), run id, model, redacted goal, prompt/recorder version, transcript sha256, evidence dir | Traceable to the run without storing the transcript |

**Deliberate constraints:**

- **A tiny template grammar.** The only placeholders are `{{inputs.x}}` and `{{secret:NAME}}` (whole value, `Fill` only). There are no expressions, so artifacts are data, never code.
- **One exception for parameterized targets.** The only locator field that may hold a template is `TableCell.row_key = {{inputs.member_id}}`. The result row is keyed by a declared, non-sensitive input, never by the member's name, which is PII. "First row" would silently pick the wrong member.
- **Content hash and approval.** The hash covers everything functional and excludes status, review, provenance and approval flags. Approving records the hash, so any later edit voids the approval and replay refuses the irreversible steps.
- **Lint reports every problem at once.** It checks that all inputs are declared and used, that each output is extracted exactly once, that irreversible steps require approval, and that no `TargetSpec` or checkpoint text contains sensitive values. For discovered artifacts it also requires every Extract step to have a non-structural strategy.
- **App profile, separate from capabilities.** One profile per vendor product holds the login sub-flow, interstitials, known dialogs, session-expiry detection, error signatures, shared business outcomes (with `scope_paths`) and sensitive labels.

## 3. Determinism & error handling

**How replay is made deterministic:** there's no model, and the same strategies are tried in the same order. Waits are bounded and come from the artifact, and every step ends with an explicit checkpoint. The key locator rule is **exactly-one**: a strategy is accepted only if it matches exactly one element. Zero or several matches fall through to the next strategy, and when nothing matches the failure lists every attempt. Strategies are also validated for uniqueness **at record time**, against the live page. In the evidence, `--repeat 5` gives 5/5 successes with identical step sequences and outputs.

**After every step, conditions are checked in a fixed order:**
1. An unknown dialog is a hard failure.
2. A known interstitial is recovered (bounded), then the sweep runs again.
3. Session expiry before any commit means log in again and **restart from step 1**. After a commit, it's `session_expired_after_commit`, and only a human can confirm what happened.
4. App error signatures are hard failures.
5. Business outcomes are returned as results.
6. The step's `expect` checkpoint is verified.

A missing target is **first explained by known states**: a NAME link that isn't there usually means NO RECORDS FOUND appeared.

| Class | Examples (Meridian Core) | Result |
|---|---|---|
| Business outcome (an answer, not an error) | member not found, access denied (403-R), deposit out of range | `business_outcome` + code, exit 10 |
| Recoverable (bounded, recorded) | maintenance interstitial, "Unposted items" confirm, re-login before commit, slow page (`slow_settle` / `settle_retry`) | `success` with `recoveries[]` |
| Hard failure (stop, debuggable) | HTTP 500 error page, target not found, checkpoint mismatch, unknown dialog, structural-only match on a risky step | `failed` + category, step, expected vs observed, screenshot + snapshot, exit 20 |
| Caller error (before the browser) | input fails the contract, artifact invalid or not approved | `failed`, no side effects, not escalated |

There are 16 failure categories, each with a fixed `retryable` flag (`cua/replay/result.py`). Irreversible steps are **at most once**: never retried, and a settle retry only re-waits, never re-clicks. A settle timeout is never swallowed.

**Drift (secondary).** If a lower-ranked strategy wins, the step is reported as `degraded`, which is a per-tenant drift signal before anything breaks. If **only** the CSS-path fallback matches, a safe step proceeds with a drift flag, but a risky or irreversible step is refused (`structural_only_on_risky`). Structure alone is never proof enough to click something that changes state. The evidence shows this on tenant B without an override.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The recorded flow speaks in roles, accessible names, labels, table positions and frames, never in Playwright calls. The seam is the `Surface` interface (observe / describe / resolve / act / screenshot).

- **Legacy web** is the implemented case: frames, tables and missing ids are handled by the scan.
- **Desktop** would be a UIA/AX Surface, which exposes the same role/name/label tree. RoleName and LabelAnchor carry over unchanged, and `CssPath` is replaced by an automation-id or tree path.
- **No-tree surfaces** (Citrix, terminal emulators) would use a vision Surface (screenshot + OCR + coordinates), with text-anchor and relative-position strategies.

In every case, the artifact, replay engine, policy, redaction and HITL stay the same.

**Multi-tenant reuse.** Hundreds of tenants run the same vendor product. The plan is to layer configuration as **vendor profile → product version range → tenant override → capability**, rather than re-recording per tenant. A `TenantOverride` patches only presentation: locators, labels, texts and timeouts. It can never change risk, approval flags, ids or the input/output contract, so approval stays tied to the base artifact. This is implemented and in the evidence: one artifact recorded on tenant A replays on tenant B, which has different labels ("ACCT HOLDER #", "SEARCH"), other branding and an extra column, through a small override (`configs/app_profiles/tenants/tenant-b.yaml`).

**Detecting and managing drift.** Every replay reports the winning strategy per step. Degraded matches per tenant and version trigger review **before** failures happen. Structural-only matches on risky steps fail closed. The fix is a tenant override, or a new MINOR/PATCH version, not a per-tenant fork. Version ranges in `app.product_versions` let a capability declare where it is valid.

**Not built:** an override registry, cross-tenant drift dashboards, path canonicalization at scale, and desktop or vision Surfaces.

## 5. Escalation & handoff

**Detecting "stuck":**
- **Discovery:** the step budget runs out, there are 3 invalid tool calls in a row, the page doesn't change after 3 actions, the model calls `escalate`, an approval is denied, or policy blocks an action.
- **Replay:** any hard failure, a structural-only match on a risky step, or an uncertain commit.

**Routing.** An intervention is written to a SQLite store (WAL mode, compare-and-set claims, so two operators can't both take one). It carries the capability and version, the step and its intent, the reason, the failure category, expected vs observed, a masked screenshot, frame paths and how to reach the live session. A minimal operator console (`cua operator`) lists interventions and offers Take control / Resume / Mark handled / Abort / Approve / Deny.

**The control-transfer model:**
- An explicit state machine: `AUTOMATION → AWAITING_HUMAN → HUMAN → RESUMING → AUTOMATION`, or `DONE`.
- A **control lease** with one owner and an epoch counter. `GuardedSurface` checks the lease before every automation action and raises `ControlNotHeld` without dispatching anything. "Who is in control" is enforced in code.
- The run process owns the state machine. The console only writes decisions to the store.

**Taking control of the live session.** The human works in the **same BrowserContext**: the headed window, or DevTools attached through a localhost-only CDP port. Tests assert that the context, the page and the session-cookie hash are identical before, during and after the handoff. A banner in the page shows the current owner. The human's actions are captured with an injected listener and redacted. They're classified by risk but not blocked (humans are the escape hatch), and they're never replayed or recorded as steps.

**Handing back.** Resume doesn't mean "continue". Automation first re-verifies where it is, in order:
1. The success checkpoint already holds → only extract the outputs.
2. The failed step's `expect` holds → continue with the next step.
3. The failed step's target resolves and the step isn't irreversible → retry it once.
4. Otherwise → escalate again, up to 2 operator rounds.

The result records a `human_intervention` recovery: operator, action count and the rule applied. Irreversible actions can also be approved from the console. The live run is recorded in `evidence/hitl/` (video, run, intervention export).

**Mocked or minimal:** the console UI has no authentication and binds to 127.0.0.1. Production would need SSO, authorization, audit, an authenticated CDP screencast instead of a raw debugging port, and routing with SLAs.

## 6. Safety

**Guardrail model (fails closed):**

- **Allowlist** (`configs/policy.yaml`): allowed origins, path globs and action types. Blocked paths beat allowed ones; the agent can't touch `/__faults` even on the app's own origin. A network filter aborts any request outside the allowlist, including ones triggered by the page itself.
- **Risk tiers and verdicts.** Reads and fills are safe; clicks are risky by default; commits are irreversible (from explicit rules plus a conservative regex on commit verbs). Each check returns ALLOW / REQUIRE_APPROVAL / BLOCK.
  - Policy checks the **live** element, and the stricter of the live and recorded decisions wins, so a drifted match can't turn SUBMIT into "CONTINUE".
  - Pressing Enter inherits the risk of the form's submit control.
- **Irreversible actions:** during discovery a human approves them (terminal or console). In replay they run only if the artifact is approved **and** its content hash still matches. They're never retried.
- **Data handling:**
  - Login runs deterministically from the profile, so the LLM never sees or types credentials. Secrets are `{{secret:NAME}}` references, resolved at fill time and registered with the redactor (case-insensitive).
  - Redaction is layered: structural (cells next to labels like SSN, ADDRESS, NAME), regex (SSN, card with Luhn check, phone, email, long account numbers), exact known values, and values learned from sensitive cells during the run. It's applied to every log write and to everything sent to the model.
  - `describe()` drops any locator strategy that would embed a sensitive value, so PII can't enter an artifact by construction.
  - Screenshot masks fail closed (the whole frame is masked if a target can't be resolved). Traces are opt-in and never committed. `scripts/scan_evidence.py` and a test scan everything committed for secrets and seeded PII.

**Limits, stated honestly:**
- Regex can miss free-text PII.
- The model does see non-sensitive page text. That's mitigated by synthetic data and by Bedrock staying inside the institution's own AWS account.
- Policy is enforced in-process, not by an OS sandbox.
- The image scanner can't read text inside screenshots, so those were checked by eye.
- The console has no authentication, and the CDP port gives full browser control (localhost only).
- Human actions are flagged, not blocked.
- Member numbers and balances aren't redacted, by design: they're the business key and the requested output.
- The IAM policy is broader than it needs to be.

## 7. Cuts

| Cut | Why | Next |
|---|---|---|
| Co-browsing operator console, auth, audit | Out of scope per the brief; the mechanism is real, the UI is minimal | Authenticated CDP screencast, SSO, audit trail, routing and SLAs |
| Desktop and vision Surfaces | Design only per the brief | A UIA/AX Surface behind the same interface |
| Multi-tenant registry and drift dashboards | Not rewarded; the seam exists (profiles + overrides) | Versioned profile/override store, drift alerts per tenant and version |
| Teaching by demonstration | Human actions are captured but not compiled into steps | Turn captured human actions into a reviewed patch to the artifact |
| Assisted fallback | Replay never calls a model | A bounded, policy-checked, single-step LLM recovery, recorded as evidence and gated by approval |
| Code generation | Low value for the evaluation | Emit a Playwright test or page object from an artifact |
| Services, queues | Not rewarded | A run service with a job queue and per-tenant isolation |
| Least-privilege IAM; output descriptions in the CLI; recording CLI argv in the manifest | Time | Small follow-ups |

**Built beyond the core:** tenant overrides (cross-tenant reuse), the multi-run stability summary (`--repeat N`), tool definitions per capability (`cua artifact tool`), and the draft → approved gate bound to the content hash.
