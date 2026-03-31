# CSA Import & Customer Association

## Problem

CSAs (Customer Success Account Managers) are not imported during account sync. Unlike sellers who map directly to customers via MSX, CSAs can't be auto-matched to specific accounts from the MSX data alone - they're pulled at the POD/territory level with no direct customer linkage.

## Solution

Import CSAs during account sync, then let the natural act of tagging them as attendees on customer calls create the customer-CSA association.

## Phase 1: Import CSAs in Account Sync

- Add a `CSA` model (or extend `SolutionEngineer` with a role/type field - TBD based on MSX data shape)
- Update the account sync to pull CSA records alongside sellers, SEs, and territories
- CSAs appear in the attendee search results (same as sellers and SEs do now)

## Phase 2: CSA-Customer Association via Attendees

- When a CSA is added as an attendee on a customer note, automatically create a `customer_csas` association (M2M or FK)
- This is implicit - the user doesn't have to explicitly "assign" a CSA to a customer, it happens naturally through call logging
- If a CSA appears on multiple customer calls, they're associated with all those customers

## Phase 3: Display on Customer View

- Show the customer's associated CSA(s) in the overview/essentials sidebar on the customer view page
- Display alongside TPID, seller, territory, DAE, verticals
- Format: "CSA: Jane Smith" with the same styling as the DAE field
- If multiple CSAs are associated (rare but possible), show all

## Open Questions

- What does the CSA data look like in MSX? Same shape as sellers/SEs or different?
- Should CSA association be removable manually, or only created/removed via attendees?
- Should we show "last call with CSA" date on the customer view?
- Do we need a separate CSA list page (like sellers list), or are they just visible on customer pages and in attendee search?
