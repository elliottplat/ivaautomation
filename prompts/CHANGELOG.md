# Terminations Prompt — Change Log

## v15 → v16

**Root cause fixed:** The v15 prompt calculated creditor entitlements correctly but
did not verify that the instructed cash movements were physically executable
given cash actually available in the case. A real case produced an instruction
that called for £807.91 of outflow (£696.14 further Nominee draw + £111.77
creditor shortfall) without checking whether that much unallocated cash existed.

**Changes:**

- **Cash Reconciliation rule (mandatory pre-instruction check):** Before any
  cashier instruction is produced, the model must reconcile cash in hand
  (received − already out) against cash required by the full-entitlement
  instruction. If cash required ≤ cash in hand the instruction proceeds as
  normal; if not, the Insufficient Funds Waterfall is applied.

- **Insufficient Funds Waterfall:** When cash in hand is insufficient, the
  model follows a strict four-step waterfall: (A) check fee position — refund
  any overdrawn fee into the case; (B) recalculate cash in hand including any
  fee refund; (C) apply remaining cash to creditors — paying shortfall in full
  if cash allows, otherwise distributing all remaining cash and flagging any
  residual as unrecoverable; (D) set output flags
  (`instruction_executable = false`, `waterfall_triggered = true`,
  `ready_to_close = true`). The waterfall explicitly prohibits further fee
  draws against insufficient cash and prohibits partial draws to "use up"
  residual cash before creditors.

- **Partial-term retention clarification:** Retention under the locked model
  cannot exceed contributions actually received. If the modification states
  "first N contributions retained" and fewer than N were received, retain only
  what was received. If a fixed £ retention exceeds total contributions
  received, retain the total received and flag in risks.

- **`cash_reconciliation` block added to JSON schema** with fields:
  `cash_received`, `cash_already_out`, `cash_in_hand_before_refunds`,
  `fee_refunds_into_case`, `cash_in_hand_after_refunds`,
  `cash_required_full_entitlement`, `instruction_executable`,
  `waterfall_triggered`, `creditor_shortfall_unrecoverable`.

- **Pre-Output Self-Check expanded** from 15 to 22 items (items 16–22 cover
  cash reconciliation, waterfall application, no over-cash fee draws, no
  "record underdraw" wording, retention cap, waterfall wording, and
  unrecoverable shortfall flagging).

- **Field generation rules updated** for `final_cashier_instruction` (uses
  waterfall-specific wording when `waterfall_triggered = true`) and
  `omni_fee_notes` (two new waterfall-triggered variants).

- **Confirmed behaviour:** A terminated IVA CAN close with an unrecoverable
  creditor shortfall. `ready_to_close` remains `true`; the shortfall is
  flagged in `risks` as unrecoverable and must not be instructed as
  recoverable.

## v14 → v15

_(pre-existing; not reconstructed here)_
