# CLAUDE.md — project rules

Computer-use automation system: an LLM discovers how to complete a goal in a UI that has no API,
the successful run is saved as a typed, versioned **capability artifact**, and that artifact is
replayed deterministically with **no LLM in the loop**. Includes human-in-the-loop handoff and
safety guardrails. `target_app/` is the hostile mock legacy bank ("Meridian Core") we automate
against.

## Rules (every change must follow these)

1. **The model discovers; artifacts are the product; replay never calls the LLM.**
2. **Surface-agnostic core.** `cua/discovery` and `cua/replay` talk to a `Surface` interface
   (observe / act / screenshot) and never to Playwright directly. Playwright web is just the first
   `Surface` implementation and lives under `cua/surface/`.
3. **No secrets or raw PII on disk.** Nothing secret or PII goes into artifacts, logs, or committed
   evidence. Everything written to disk goes through the redactor (`cua/policy`, built in a later
   step).
4. **Typed everything.** Data that crosses a module boundary is a pydantic model. Keep modules
   small. Schema, locator and replay logic must have tests.
5. **Deterministic, explicit behavior over cleverness.** Every failure carries step id, expected
   and observed.
6. **Pluggable LLM provider.** Discovery talks only to an `LLMClient` interface
   (`cua/discovery/llm.py`). The first implementation uses the AWS Bedrock Converse API (boto3,
   tool use), so any Bedrock model works by changing `BEDROCK_MODEL_ID`. A `ScriptedLLMClient`
   (replays canned tool calls) exists for tests. Replay must never import the LLM module, and
   running replay must require no cloud credentials.

## Conventions

- Python 3.11, managed with `uv`. Use the Playwright **sync** API everywhere.
- Lint: `uv run ruff check .` — Tests: `uv run pytest`.
- Config lives in `configs/` (policy.yaml, app_profiles/). Saved artifacts go in `capabilities/`
  (JSON). Per-run output goes in `runs/` (gitignored). Only redacted demo evidence goes in
  `evidence/`.
- `.env` is gitignored. AWS credentials come from the AWS profile (`AWS_PROFILE`), never from
  `.env`.

## Target app (Meridian Core)

- Run: `uv run python -m target_app --port 8001 --tenant a` (or `--tenant b`).
- All tenant differences live in `target_app/tenants.py::TENANTS`.
- Fault injection (no auth): `GET/POST /__faults`, `POST /__faults/reset`. Faults are
  counter-based and never random. They only affect main-frame pages (`/welcome`, `/inquiry`,
  `/member/*`, `/subacct/*`).
- The app is deliberately hostile: there are no ids, test ids, aria attributes or `<label>`s.
  Do not "fix" that, because it is the point of the exercise.
