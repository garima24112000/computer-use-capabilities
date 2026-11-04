> **Rejected by today's lint** (`cua artifact show` exits 1). Rendered below with lint bypassed, for history only.
>
> ```
> INVALID capabilities/meridian.subaccount.open/1.0.0.json
> 1 validation error for Capability
>   Value error, 1 lint error(s):
> [extract_structural_only] steps[7:s08_extract_confirmation_number].action.target: extract target has only css_path strategies; it needs a TableCell, LabelAnchor or stable TextMatch (with the output's extract_pattern) [type=value_error, input_value={'app': {'product': 'meri...t'], 'version': '1.0.0'}, input_type=dict]
>     For further information visit https://errors.pydantic.dev/2.14/v/value_error
> ```

# Open a sub-account for a member

`meridian.subaccount.open` v1.0.0 · status **draft** · app meridian-core `>=4.2,<5` (profile `meridian-core`, web)
content hash `sha256:02e779854333aaf7b3fe708a28078afb1aef7f025186dc9ec6d6f8f14d2a6d10`

Open a new Money Market sub-account for member 22222 with an initial deposit of 250.00 and nickname Rainy Day, and reach the confirmation screen

## Inputs

| name | type | required | constraints | sensitive | description |
|---|---|---|---|---|---|
| `member_id` | string | yes | pattern `^\d{5}$` | no | member id |
| `account_type` | enum | yes | one of `Share Savings`, `Money Market`, `Certificate 12mo` | no | account type |
| `initial_deposit` | decimal | yes | - | no | initial deposit |
| `nickname` | string | yes | - | no | nickname |

## Outputs

| name | type | normalize | sensitive | description |
|---|---|---|---|---|
| `confirmation_number` | string | none | no | confirmation number |

## Preconditions

- entry `/login`; session: profile login subflow runs first

## Steps

1. **`s01_click_open_sub_account`**: Navigate to the Open Sub-Account screen to begin creating a new {{inputs.account_type}} sub-account.
   - action: click link "OPEN SUB-ACCOUNT" in section FUNCTIONS [frame nav]
   - risk: **risky**
   - frame `nav`; locators: role_name → text_match → css_path
   - expect:
     - postcondition recorded during discovery (timeout 5000 ms)
       - all of: url_matches[main] `/subacct/open`
       - all of: text_present[main] "OPEN SUB-ACCOUNT"
2. **`s02_fill_member_no`**: Enter the member ID to identify the account holder.
   - action: fill `{{inputs.member_id}}` into textbox "MEMBER NO." in section OPEN SUB-ACCOUNT [frame main]
   - risk: **safe**
   - frame `main`; locators: role_name → label_anchor('MEMBER NO.') → css_path
3. **`s03_select_account_type`**: Select {{inputs.account_type}} as the account type for the new sub-account.
   - action: select `{{inputs.account_type}}` in combobox "ACCOUNT TYPE" in section OPEN SUB-ACCOUNT [frame main]
   - risk: **safe**
   - frame `main`; locators: role_name → label_anchor('ACCOUNT TYPE') → css_path
4. **`s04_fill_initial_deposit`**: Enter the initial deposit amount for the new {{inputs.account_type}} sub-account.
   - action: fill `{{inputs.initial_deposit}}` into textbox "INITIAL DEPOSIT" in section OPEN SUB-ACCOUNT [frame main]
   - risk: **safe**
   - frame `main`; locators: role_name → label_anchor('INITIAL DEPOSIT') → css_path
5. **`s05_fill_nickname`**: Enter the nickname '{{inputs.nickname}}' for the new {{inputs.account_type}} sub-account.
   - action: fill `{{inputs.nickname}}` into textbox "NICKNAME" in section OPEN SUB-ACCOUNT [frame main]
   - risk: **safe**
   - frame `main`; locators: role_name → label_anchor('NICKNAME') → css_path
6. **`s06_click_continue`**: Submit the form to proceed to the confirmation screen for the new {{inputs.account_type}} sub-account.
   - action: click button "CONTINUE" in section OPEN SUB-ACCOUNT [frame main]
   - risk: **risky**
   - frame `main`; locators: role_name → text_match → css_path
   - expect:
     - postcondition recorded during discovery (timeout 5000 ms)
       - all of: url_matches[main] `/subacct/review`
       - all of: text_present[main] "OPEN SUB-ACCOUNT - REVIEW"
7. **`s07_click_submit`**: Submit the reviewed sub-account details to reach the confirmation screen.
   - action: click button "SUBMIT" in section OPEN SUB-ACCOUNT - REVIEW [frame main]
   - risk: **irreversible** · **requires approval** (NOT approved)
   - frame `main`; locators: role_name → text_match → css_path
   - expect:
     - postcondition recorded during discovery (timeout 5000 ms)
       - all of: url_matches[main] `/subacct/submit`
       - all of: text_present[main] "OPEN SUB-ACCOUNT - CONFIRMATION"
8. **`s08_extract_confirmation_number`**: Extract the confirmation number from the confirmation screen.
   - action: extract text "<value>" in section OPEN SUB-ACCOUNT - CONFIRMATION [frame main] → output `confirmation_number`
   - risk: **safe**
   - frame `main`; locators: css_path

## Business outcomes

Inherited from profile `meridian-core`: `member_not_found`, `invalid_member_format`, `access_denied`, `deposit_out_of_range`, `invalid_deposit_amount`

## Success

- final page shows the extracted values (timeout 5000 ms)
  - all of: url_matches[main] `/subacct/submit`
  - all of: text_present[main] "OPEN SUB-ACCOUNT - CONFIRMATION"
  - all of: target_present[main] text "<value>" in section OPEN SUB-ACCOUNT - CONFIRMATION [frame main] (css_path)

## Provenance

- source: discovery; recorder recorder-v1+discovery-v1 (cua 0.1.0); model `us.anthropic.claude-sonnet-4-6`
- goal (redacted): Open a new Money Market sub-account for member 22222 with an initial deposit of 250.00 and nickname Rainy Day, and reach the confirmation screen
- run `20261103T174137Z-7b68e0` at 2026-11-03 17:41:37.984019+00:00; evidence `runs/20261103T174137Z-7b68e0`; transcript sha256 `93e6a90e8919b4401c79ced76ed5604a51e0d356da51a3d7c4a184e08f619b13`
- git commit `24f5e6ea24dd143f1b45a4e5c835183f3ff699bc`
- review: not reviewed
