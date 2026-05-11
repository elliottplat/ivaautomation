# Terminations Prompt — Change Log

## v16 → v17

**Root cause addressed:** The v16 prompt had no mechanism for cases where the
EOS is derived from a VMOC (Variation Meeting of Creditors) rather than a
standard modification agreement. Three distinct EOS states are now recognised
and handled separately throughout the analysis.

**Changes:**

- **Three-state EOS input (`eos_state`):** The `analyze_termination` endpoint
  now accepts an `eos_state` field with three valid values:
  - `NON_VMOC` — standard EOS, no VMOC involvement (default, prior behaviour)
  - `VMOC_AGREED` — EOS is from an agreed and signed-off VMOC
  - `VMOC_UNAGREED` — EOS is an outline of a proposed VMOC not yet agreed

- **Fifth document for VMOC_UNAGREED:** When `eos_state = VMOC_UNAGREED`, a
  VMOC Modifications document must be uploaded as a fifth input. The backend
  returns HTTP 400 if this document is missing.

- **State-conditional DOCUMENT PRIORITY:** Three separate document priority
  lists are defined — one for each `eos_state`. For `VMOC_UNAGREED`, the VMOC
  Modifications document takes precedence over the pre-existing Modifications
  and the outline EOS has no authority over the locked model.

- **MODIFICATION READING RULE extended for VMOC_UNAGREED:** Both the
  pre-existing Modifications and the VMOC Modifications documents must be read
  in full. The VMOC Modifications govern where they conflict. Any displaced
  clause must be noted in `risks` and logged in `modification_conflicts_resolved`.
  No calculation figure may be sourced from the outline EOS.

- **Mandatory provisional risk flag (VMOC_UNAGREED):** When
  `eos_state = VMOC_UNAGREED`, the following risk entry is always included
  regardless of all other conditions: *"Calculation based on unagreed VMOC
  outline; figures provisional pending VMOC approval. Re-run required if VMOC
  terms change before agreement."*

- **JSON schema additions:**
  - `eos_state` added as a top-level field
  - `vmoc_modifications_applied` added to `locked_model` (list of VMOC clause
    changes applied)
  - `modification_conflicts_resolved` added to `locked_model` (list of
    conflicts between pre-existing and VMOC modifications and how resolved)

- **Pre-Output Self-Check expanded** from 22 to 27 items (items 23–27 cover
  `eos_state` validity, VMOC Mods read in full, no EOS-sourced figures, mandatory
  provisional flag present, correct document priority applied).

- **Web app UI:** `terminations.html` updated with a two-question VMOC
  disclosure flow (Q1: is EOS from VMOC? Q2: is it agreed?) that maps to the
  three states. A conditional upload card for the VMOC Modifications document
  appears only when `VMOC_UNAGREED` is selected. The `eos_state` field is
  submitted alongside existing form fields.

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
