# Discovery history: 1.0.0 → review → recorder fixes → 1.1.0

The first real discovery runs (Bedrock, `us.anthropic.claude-sonnet-4-6`) produced
`meridian.member.read_savings_balance@1.0.0` and `meridian.subaccount.open@1.0.0`. Both runs
**completed**: the model reached the goal and the replayable artifacts were saved. A human then
reviewed both artifacts against the hand-written fixtures and found **five generalization
gaps**. Each gap was fixed in the **recorder, CLI and profile**. Neither 1.0.0 artifact was
hand-edited; both stay in `capabilities/` as history. Re-running discovery produced 1.1.0.

| Run | Capability | Turns | Tokens (in / out) | Wall | Output |
|---|---|---|---|---|---|
| `20261103T171959Z-7374a1` | read_savings_balance 1.0.0 | 6 | 17,528 / 446 | 10.9 s | `savings_balance = 12345.67` |
| `20261103T174137Z-7b68e0` | subaccount.open 1.0.0 | 9 | 28,704 / 718 | 27.5 s | `SUB-ACCOUNT OPENED — CONFIRMATION NO. MC-539968` (whole sentence: gap 2) |

Per run: `summary.json`, `manifest.json`, `artifact_show_1.0.0.md` (`cua artifact show`) and
the diff against 1.1.0:

- [read_savings_balance/show_1.0.0_vs_1.1.0.diff](read_savings_balance/show_1.0.0_vs_1.1.0.diff)
- [subaccount_open/show_1.0.0_vs_1.1.0.diff](subaccount_open/show_1.0.0_vs_1.1.0.diff)

`subaccount.open@1.0.0` is now **rejected** by the `extract_structural_only` lint (gap 2).
`cua artifact show` exits 1 on it, so its show file starts with that rejection and is rendered
with lint bypassed.

## The five gaps and their fixes

1. **The description contained run data.** The 1.0.0 description was the raw goal ("Look up
   member 12345 …", "… member 22222 … nickname Rainy Day …").
   - **Fix:** `cua discover --description`, which defaults to the title. Any free text the
     recorder writes has input literals replaced by `{{inputs.x}}`. New lint warning:
     `input_literal_in_text`.
2. **The extract target was structural only.** The subaccount confirmation was located by
   `css_path` alone, and the output was the whole sentence.
   - **Fix:** the recorder tries TableCell → LabelAnchor → stable TextMatch, combined with the
     output's `extract_pattern` (`(MC-\d{6})`). New lint error: `extract_structural_only`.
3. **Business outcomes were not scoped.** The read capability inherited the deposit outcomes.
   - **Fix:** `OutcomeSpec.scope_paths` in the app profile. The recorder keeps only outcomes
     whose scope matches a page visited in the run.
4. **INQUIRE had no postcondition.** The URL doesn't change at that step.
   - **Fix:** when neither the URL nor a section header changes, the recorder builds the expect
     from newly appeared stable content (the result grid's NAME / BRANCH column headers).
5. **The savings extract keyed on the suffix `00` only.**
   - **Fix:** the business label `Share Savings` is the first row key, with `00` as fallback,
     as in the fixture.

The CLI also gained a richer contract at the same time, which shows in the subaccount diff:
numeric bounds (`initial_deposit` 0.01..10000), per-input descriptions, an optional `nickname`
and output extract patterns.
