# Read a member's share savings balance

`meridian.member.read_savings_balance` v1.0.0 · status **draft** · app meridian-core `>=4.2,<5` (profile `meridian-core`, web)
content hash `sha256:4d05be5dc9796c5c8a26eeafa087b7be99667e7025307ab59687bb4b4f59887b`

Looks up a member by member number and returns the balance of the base Share Savings account (suffix 00). HAND-WRITTEN TEST FIXTURE: real artifacts come from discovery.

## Inputs

| name | type | required | constraints | sensitive | description |
|---|---|---|---|---|---|
| `member_id` | string | yes | pattern `^\d{5}$` | no | 5-digit member number |

## Outputs

| name | type | normalize | sensitive | description |
|---|---|---|---|---|
| `savings_balance` | money | money_to_decimal | no | Balance of the Share Savings account (suffix 00) as a decimal string, e.g. 12345.67 |

## Preconditions

- entry `/login`; session: profile login subflow runs first

## Steps

1. **`s01_open_member_inquiry`**: Open the member inquiry screen from the left menu.
   - action: click MEMBER INQUIRY menu item
   - risk: **risky**
   - frame `nav`; locators: role_name → text_match
   - expect:
     - inquiry screen loaded (timeout 5000 ms)
       - all of: url_matches[main] `/inquiry*`
2. **`s02_fill_member_no`**: Type the member number into the MEMBER NO. field.
   - action: fill `{{inputs.member_id}}` into MEMBER NO. field
   - risk: **safe**
   - frame `main`; locators: label_anchor('MEMBER NO.') → role_name
3. **`s03_click_inquire`**: Run the inquiry; either the result row appears or the core answers with a business outcome.
   - action: click INQUIRE button
   - risk: **risky**
   - frame `main`; locators: role_name → text_match
   - expect:
     - result row for the member is listed (timeout 5000 ms)
       - all of: target_present[main] NAME link in the result row for the member (table_cell)
4. **`s04_open_member_detail`**: Open the member detail page via the name link in the result row keyed by the member number.
   - action: click NAME link in the result row for the member
   - risk: **risky**
   - frame `main`; locators: table_cell(row='{{inputs.member_id}}', col='NAME'); keyed by `{{inputs.member_id}}`
   - expect:
     - member detail page for the member (timeout 5000 ms)
       - all of: url_matches[main] `/member/detail*`
       - all of: text_present[main] "MEMBER DETAIL - {{inputs.member_id}}"
   - notes: The row is keyed by the member number (a declared, non-sensitive input), never by the member's name, which is PII.
5. **`s05_extract_savings_balance`**: Read the BALANCE of the Share Savings row in the ACCOUNTS table.
   - action: extract Share Savings BALANCE cell in ACCOUNTS → output `savings_balance`
   - risk: **safe**
   - frame `main`; locators: table_cell(row='Share Savings', col='BALANCE') → table_cell(row='00', col='BALANCE')
   - notes: Fallback keys on suffix 00: if a second Share Savings sub-account exists, the first strategy is ambiguous and resolution falls through to the base account (reported as degraded).

## Business outcomes

Inherited from profile `meridian-core`: `member_not_found`, `invalid_member_format`, `access_denied`

## Success

- member detail with an ACCOUNTS table is shown (timeout 5000 ms)
  - all of: url_matches[main] `/member/detail*`
  - all of: text_present[main] "MEMBER DETAIL - {{inputs.member_id}}"
  - all of: text_present[main] "ACCOUNTS"

## Provenance

- source: hand_written; recorder hand-written
- goal (redacted): Read the share savings balance for a member
- review: not reviewed
