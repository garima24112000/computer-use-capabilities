# HITL live handoff: what the video shows

[`hitl_handoff.mp4`](hitl_handoff.mp4) is a recording with voiceover of the run in [`run/`](run/)
(`20261104T013251Z-964956`). The intervention it creates is exported in
[`intervention.json`](intervention.json).

## Setup

- **Terminal 1:** `uv run cua operator`, which starts the console on http://127.0.0.1:8700/.
- **Terminal 2:**

```
uv run cua replay meridian.member.read_savings_balance --input member_id=12345 --hitl \
  --inject-at s04_click_name '{"http500_count": 1, "http500_path_prefix": "/member"}'
```

## Timeline

| Time | What happens |
|---|---|
| 0:12 | The replay opens a headed Chromium window (`--hitl`) and signs in through the login subflow. |
| 0:19 | The harness fault fires at `s04_click_name`, and the app shows **SYSTEM ERROR 0xC0FFEE** (HTTP 500). Replay classifies it as `app_error` with the profile signature `system_error_coffee`. |
| 0:56 | The console queue shows the `replay_failure` intervention. Control is **AWAITING_HUMAN**, and the banner in the browser reads "WAITING FOR AN OPERATOR". The detail page shows the step, expected vs observed, the masked screenshot, frame paths and the CDP URL. |
| 1:18 | The operator types their name and clicks **Take control**. The intervention is claimed with a compare-and-set, so a second claim would get 409. |
| 1:24 | The banner reads **HUMAN IN CONTROL - Garima**. Automation now holds no lease, and any automation action would raise `ControlNotHeld`. |
| 1:35 | Working in the same browser window, the operator does the lookup by hand: MEMBER INQUIRY → `12345` → INQUIRE → the member's name. The session cookie and page are the same ones the run used (see the `session_check` events in `run/events.jsonl`). |
| 2:11 | The console's captured actions list the click on the member's name as `[REDACTED:NAME]`. |
| 2:30 | The operator clicks **Resume**. |
| 2:36 | Replay verifies the page (rule `success_checkpoint`: the whole success checkpoint already holds), runs only the remaining Extract step, and prints **SUCCESS** with `savings_balance = 12345.67`. |
| 2:41 | The result lists recovery `human_intervention:iv-47808e0f30 at s04_click_name operator Garima; 11 human action(s); round 1; rule success_checkpoint`. |
| 2:54 | `uv run cua interventions list` shows the intervention as `resolved`, `res=resumed`. |

## Files

| File | What it is |
|---|---|
| [`run/result.json`](run/result.json) | The ReplayResult: success; step statuses `ok ok ok human ok`; `irreversible_executed_by_human: false`. |
| [`run/events.jsonl`](run/events.jsonl) | Control transitions (escalate → take_control → resume → verified), `human_action` events (actor `human`, redacted) and `session_check` events. |
| `run/screenshots/`, `run/snapshots/` | Per-step screenshots and the failure screenshot and snapshot. All screenshots are masked: PII and the operator id are painted magenta. |
| `run/live.png` | The last masked live view the console showed. |
| [`intervention.json`](intervention.json) | The intervention, exported from `runs/_hitl/interventions.db`. Everything in it was redacted when it was written. |
| [`console_example.png`](console_example.png) | The operator console detail page, rendered by `tests/hitl/test_handoff.py` from a fresh test run (operator "alice"). It shows 7 captured actions and "3 click(s) on empty areas ignored". |

## Note on the captured actions

In this recording, actions 1–3 in `intervention.json` (`click main`, `click top`,
`click main`) were clicks on empty frame areas. Capture has since been changed: a click that
lands on neither an interactive element nor a named text element is no longer listed as an
action, only counted as `ignored_clicks`. `console_example.png` shows the new behavior. The
export is kept exactly as recorded.
