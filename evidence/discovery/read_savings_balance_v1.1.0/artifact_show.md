# Read a member's share savings balance

`meridian.member.read_savings_balance` v1.1.0 · status **approved** · app meridian-core `>=4.2,<5` (profile `meridian-core`, web)
content hash `sha256:52a72ca413bc30b1fd03ccccf237b2101ed106a7ed43467cbc4d5ff914568f4a`

Looks up a member by member number and returns the balance of the base Share Savings account.

## Inputs

| name | type | required | constraints | sensitive | description |
|---|---|---|---|---|---|
| `member_id` | string | yes | pattern `^\d{5}$` | no | 5-digit member number |

## Outputs

| name | type | normalize | sensitive | description |
|---|---|---|---|---|
| `savings_balance` | money | money_to_decimal | no | savings balance |

## Preconditions

- entry `/login`; session: profile login subflow runs first

## Steps

1. **`s01_click_member_inquiry`**: Open the Member Inquiry screen to look up member {{inputs.member_id}}.
   - action: click button "MEMBER INQUIRY" in section FUNCTIONS [frame nav]
   - risk: **risky**
   - frame `nav`; locators: role_name → text_match → css_path
   - expect:
     - postcondition recorded during discovery (timeout 5000 ms)
       - all of: url_matches[main] `/inquiry`
       - all of: text_present[main] "MEMBER INQUIRY"
2. **`s02_fill_member_no`**: Enter the member ID to look up member {{inputs.member_id}}.
   - action: fill `{{inputs.member_id}}` into textbox "MEMBER NO." in section MEMBER INQUIRY [frame main]
   - risk: **safe**
   - frame `main`; locators: role_name → label_anchor('MEMBER NO.') → css_path
3. **`s03_click_inquire`**: Submit the member ID to retrieve member {{inputs.member_id}}'s account information.
   - action: click link "INQUIRE" (label MEMBER NO.) in section MEMBER INQUIRY [frame main]
   - risk: **risky**
   - frame `main`; locators: role_name → label_anchor('MEMBER NO.') → text_match → css_path
   - expect:
     - postcondition recorded during discovery (timeout 5000 ms)
       - all of: target_present[main] column header 'NAME' (role_name)
       - all of: target_present[main] column header 'BRANCH' (role_name)
4. **`s04_click_name`**: Open member {{inputs.member_id}}'s account details to find their Share Savings balance.
   - action: click link "[REDACTED:NAME]" (label {{inputs.member_id}}) in section MEMBER INQUIRY [frame main]
   - risk: **risky**
   - frame `main`; locators: table_cell(row='{{inputs.member_id}}', col='NAME') → css_path; keyed by `{{inputs.member_id}}`
   - expect:
     - postcondition recorded during discovery (timeout 5000 ms)
       - all of: url_matches[main] `/member/detail`
       - all of: text_present[main] "MEMBER DETAIL - {{inputs.member_id}}"
5. **`s05_extract_savings_balance`**: Extract the Share Savings balance for member {{inputs.member_id}}.
   - action: extract cell "<value>" in section ACCOUNTS [frame main] → output `savings_balance`
   - risk: **safe**
   - frame `main`; locators: table_cell(row='Share Savings', col='BALANCE') → table_cell(row='00', col='BALANCE') → css_path

## Business outcomes

Inherited from profile `meridian-core`: `member_not_found`, `invalid_member_format`, `access_denied`

## Success

- final page shows the extracted values (timeout 5000 ms)
  - all of: url_matches[main] `/member/detail`
  - all of: text_present[main] "MEMBER DETAIL - {{inputs.member_id}}"
  - all of: target_present[main] cell "<value>" in section ACCOUNTS [frame main] (table_cell → table_cell → css_path)

## Provenance

- source: discovery; recorder recorder-v1+discovery-v1 (cua 0.1.0); model `us.anthropic.claude-sonnet-4-6`
- goal (redacted): Look up member 12345 and read their Share Savings balance
- run `20261103T232133Z-5d322d` at 2026-11-03 23:21:33.003851+00:00; evidence `runs/20261103T232133Z-5d322d`; transcript sha256 `ce779facf498e5be5057fc88122ca56a2dce65e7d3e911705084c66553640df7`
- git commit `2c30cff29a235b40ecf4c9a7c583c5e88c0b1cfd`
- review: approved by Garima at 2026-11-04 05:46:18+00:00: Reviewed contract, locators, checkpoints; no irreversible steps
