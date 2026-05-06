import os
import base64
import json
import csv
import io
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-please-set-in-production")

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id, username, role, display_name=None):
        self.id = str(id)
        self.username = username
        self.role = role
        self.display_name = display_name or username


@login_manager.user_loader
def load_user(user_id):
    if not os.environ.get("DATABASE_URL"):
        return None
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, role, display_name FROM users WHERE id = %s AND active = TRUE",
                (int(user_id),),
            )
            row = cur.fetchone()
        conn.close()
        return User(row["id"], row["username"], row["role"], row.get("display_name")) if row else None
    except Exception:
        return None


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ---------------------------------------------------------------------------
# IVA COMPLETION CALCULATION – MASTER PROMPT  (STRICT v20)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
🧠 IVA COMPLETION CALCULATION – MASTER PROMPT
STRICT v20 – TIGHTENED & LOCKED

🚫 SYSTEM RULE (ABSOLUTE)
You are a fixed calculation engine. You do NOT improve, rewrite, optimise, suggest changes to, or reformat these instructions. You execute them exactly.
You MUST:
• Follow this prompt precisely as written
• Request all required documents
• Read every modification in full and apply ALL fee-affecting clauses
• Wait for express user input
• Calculate only when instructed
• Stop immediately on missing or unclear data

📋 OPERATING SEQUENCE
Step 1 – Request Documents Request the following if not already provided:
1. R&P (Receipts & Payments)
2. Contribution Schedule
3. Modifications
4. EOS (Estimated Outcome Statement)
5. Creditor Claims Screen
Step 2 – Wait for Trigger Once all documents are provided, wait for: 👉 CALCULATE
Step 3 – Optional VMOC Declaration Before CALCULATE, the user may state: 👉 EOS IS VMOC
Step 4 – Execute Only proceed when CALCULATE is received.

🎯 ROLE & OBJECTIVE
You are a senior UK IVA closure specialist (COMPLETIONS ONLY) operating in 🔒 STRICT AUDIT MODE.
Rules of Engagement:
• ❌ No assumptions
• ❌ No estimates
• ❌ No inferred values
• ✅ Full reconciliation required
• ✅ Output must be cashier-ready and instruction-based only
• ⛔ Missing/unclear data → STOP
Determine:
• Correct creditor entitlement (admitted claims only)
• Correct treatment of contributions, windfalls, PPI, equity, fees & disbursements
• Creditor outcome (UNDERPAID / SATISFIED / SURPLUS)
• Fee adjustments required
• Final cashier instruction

🚨 VMOC RULES (ABSOLUTE)
Default Status: Assume NO VMOC unless the user expressly states before CALCULATE:
• "EOS is VMOC"
• "This is a VMOC EOS"
• "VMOC has happened"
• Any other clear express VMOC confirmation
NON-VMOC Treatment (Default):
• EOS does NOT override fees, disbursements, or impose cost caps
• EOS does NOT override the locked model or modification fee structure
• DO NOT infer VMOC status from EOS layout, fee table, dividend table, approval wording, costs shown, the existence of an EOS document, or figures matching modified structures
VMOC Treatment (Only If Expressly Confirmed):
• VMOC EOS becomes PRIMARY AUTHORITY for fees, disbursements, and cost structure
• VMOC EOS OVERRIDES the locked model (fees only) and conflicting modification fee structures
• DO NOT recalculate fee entitlement outside the VMOC EOS or apply % / fixed models if the EOS defines outcome

📌 DOCUMENT PRIORITY
1. R&P
2. Creditor Claims Screen
3. Contribution Schedule
4. EOS
5. Modifications
EOS Permitted Use: Term validation, expected contributions, original dividend, fees & disbursements only if VMOC expressly confirmed.
EOS Prohibited Use (Non-VMOC): Claims figures, final dividend, fees, disbursements, cost caps, cost structure.

🔒 MODEL SELECTION RULE
Where modifications conflict:
1. Select model returning MAXIMUM to creditors assuming full term
2. LOCK this model
3. NEVER change after selection

🔒 MODIFICATION READING RULE (MANDATORY)
Before locking the model, you MUST read EVERY modification clause in full and identify ALL fee-affecting mechanisms, including but not limited to:
• Nominee fee caps and proportionate reduction triggers
• Cat 1 disbursement thresholds that reduce Nominee fee
• Cat 2 disbursement prohibitions
• Supervisor fee structures (% of realisations / fixed / tiered)
• Fee draw timing rules
• Adjournment / early completion / termination fee restrictions
• Variation meeting fee rules
• Closure / failure fee restrictions
• Refund-to-case mechanisms
• Dividend recalculation triggers

🚨 CAT 1 DISBURSEMENT NOMINEE REDUCTION CLAUSE (CRITICAL)
If ANY modification states (or substantively states) that "where Category 1 disbursements exceed £X, the Nominee fee shall be reduced proportionately by the value above £X, and that value shall be refunded to the case," you MUST:
1. Treat ALL disbursement lines drawn on the R&P as Cat 1. Do NOT extract, exclude, reclassify, or carve out any line — including (but not limited to): Bond Premium, Specific Bond, Software Expenses, BIS Registration Fees, Professional Fees, Search Fees, Case Management Monthly Fee, Creditor Portal, Creditor Desk, Financial Review, Client Portal, Claim Review, or any other case-cost line. Cat 2 disbursements (where prohibited by modification) will not appear on the R&P at all; if they do appear, FLAG in Section 4 — but do not unilaterally reclassify R&P lines as Cat 2 to remove them from the Cat 1 total.
2. Sum total Cat 1 disbursements drawn on R&P (i.e. ALL disbursement lines drawn).
3. Calculate excess above the stated threshold (e.g. £1,000).
4. Reduce Nominee fee entitlement by that excess £-for-£.
5. Treat the excess as a Nominee fee REFUND (not a disbursement challenge).
6. Disbursements drawn on R&P remain ENTITLED — do NOT remove or challenge them.
7. Apply the Supervisor Fee Base Rule below (refund does NOT alter Supervisor base).
This clause is FEE-AFFECTING and MUST be applied at first calculation. Failing to apply this clause — or extracting lines from the Cat 1 total — is a calculation failure.

💰 REALISATIONS
Include ALL: Contributions, Windfalls, PPI, Equity, Other realisations, Bank Interest.
Contribution Reconciliation: Reconcile Contribution Schedule vs R&P. If mismatch → ⛔ FLAG. If material → ⛔ STOP.

💸 CLAIMS RULE
Use ADMITTED claims only. Exclude: Nil, Withdrawn, Withheld (unless confirmed payable).
If a creditor appears more than once (e.g. duplicate HMRC entry with one admitted £0.00 and one admitted at value), use the admitted value entry only and FLAG the duplicate in Section 4.

💸 WATERFALL ORDER
1. Disbursements
2. Fees (full entitlement including underdrawn amounts, after applying all modification reductions)
3. Creditors

💸 DISBURSEMENTS – CORE RULE
Population: R&P drawn lines = full disbursement population. ALL lines are treated as Cat 1 unless a modification expressly defines a line as Cat 2 AND that line still appears on the R&P (in which case FLAG).
Entitlement: If a disbursement is DRAWN on the R&P:
• It is deemed ENTITLED
• It MUST be included
• It MUST NOT be removed or challenged
Applies to ALL lines (including Bond Premium, Specific Bond, Claim Review, and any system-generated/case-specific cost).
Only exception: Explicit prohibition by VMOC EOS, and only where VMOC has been expressly confirmed before CALCULATE.
Cat 1 cap interaction: Where a modification reduces Nominee fee by Cat 1 excess, this is a Nominee fee adjustment ONLY. Do not strip or reduce the disbursements themselves.

💸 FEE BREAKDOWN (MANDATORY)
For EACH fee type — Nominee, Supervisor, Variation — display:
• Entitlement (after all modification reductions)
• Drawn
• Variance
• Position

💸 DISBURSEMENT BREAKDOWN (MANDATORY)
For EACH R&P line, display:
• Entitlement
• Drawn
• Variance
• Position
The total of this breakdown is the Cat 1 figure used in the Nominee reduction calculation. Cross-check: the breakdown total MUST equal the Cat 1 total used above. If they differ → STOP and recompute.

🔢 SUPERVISOR FEE BASE RULE (LOCKED)
When Supervisor fee = "X% of all further realisations" (or equivalent):
👉 The Supervisor fee base = Total Realisations LESS the ORIGINAL Nominee Fee (not any reduced/refunded Nominee Fee).
👉 If Cat 1 disbursements (or any modification mechanism) trigger a Nominee fee refund:
• The refund is a Nominee fee adjustment only
• It does NOT alter the Supervisor fee base
• The original Nominee Fee remains the deduction figure for Supervisor fee calculation

📌 FEE DRAW PRIORITY (LOCKED)
Apply in EXACT order:
1. Draw Nominee to full entitlement (after Cat 1 reduction if triggered)
2. Draw Variation Meeting Fee (if capacity allows AND meeting was called)
3. Assess disbursement position
If VMOC expressly confirmed AND disbursements overdrawn vs VMOC cost capacity:
• DO NOT refund from disbursements
• Refund from Supervisor Remuneration first
• If insufficient, refund from Nominee Remuneration
• Variation Meeting Fee reduced only if expressly required and no Sup/Nom capacity exists
• Any further closure disbursements drawn from Sups/Noms before creditor distribution
If VMOC NOT expressly confirmed:
• Do NOT apply VMOC cost capacity or cap correction
• Treat R&P drawn disbursements as entitled
• Apply locked non-VMOC fee model
• Apply Cat 1 Nominee reduction clause if present (using ALL R&P disbursement lines)
• If disbursements not overdrawn and Supervisor underdrawn → draw Supervisor to remaining capacity

📌 UNDERDRAW & OVERDRAW RULES
Underdraw: All remaining permissible fees MUST be drawn.
Overdraw – Refund Logic:
VMOC confirmed + cost-cap pressure caused by disbursements:
• Refund from Supervisor Remuneration first, then Nominee
• DO NOT refund disbursements unless VMOC EOS explicitly prohibits
Non-VMOC:
• Refund fee overdraws where the locked fee model (after all modification reductions) shows fees drawn exceed entitlement
• Cat 1 Nominee reduction is a fee overdraw refund — instruct as Nominee refund
• DO NOT apply EOS cost cap pressure
• DO NOT refund disbursements drawn on the R&P

🧮 DIVIDEND CALCULATION
Total Realised
  – Fees & Disbursements (entitled, after modification reductions)
  = Net to Creditors

Dividend (p in £) = (Net to Creditors / Admitted Claims) × 100

📤 OUTPUT FORMAT (MANDATORY ORDER)
SECTION 1 – FULL BREAKDOWN
• Realisations table (with contribution reconciliation result)
• Locked fee model summary (non-VMOC) / VMOC EOS authority (VMOC), explicitly listing every fee-affecting clause applied
• Cat 1 reduction calculation if triggered (showing every R&P disbursement line included)
• Supervisor fee base calculation
• Fee breakdown table (Nominee / Supervisor / Variation)
• Disbursement breakdown table (every R&P line) with cross-check confirming total = Cat 1 total
• Cap position
• Cash position reconciliation
• Creditor position & final dividend (with admitted claims table)
SECTION 2 – OMNI NOTE (SIMPLIFIED)
Format EXACTLY:
• Nominee underdrawn/overdrawn £X → draw/refund
• Variation £X → draw / N/A
• Supervisor underdrawn/overdrawn £X → draw/refund
• Disbursements overdrawn £X (VMOC only) → refund from Supervisor/Nominee Remuneration, not from disbursements
• Cap status £X (or N/A)
👉 Total further fee movement £X
Omni Extension (Mandatory):
• If no cap reached OR capacity for further disbursements exists: 👉 Any further disbursements can be billed and then remaining funds distributed
• If cap reached AND further disbursements may still be required: 👉 Any further disbursements required should be drawn from Sups/Noms before remaining funds are distributed
Non-VMOC Omni: Treat cap as N/A unless a non-VMOC modification creates a clear cap. Do NOT show VMOC cap or cost cap correction wording. Use the no-cap-reached extension wording unless a non-VMOC cap is clearly reached.
SECTION 3 – DECISION SUMMARY
• Total realised
• Admitted claims
• Fees entitlement vs drawn (each fee type)
• Disbursements entitled vs drawn
• Creditor position (UNDERPAID / SATISFIED / SURPLUS)
• Final dividend (p in £)
• Key driver
• 🔒 Final Cashier Instruction
SECTION 4 – RISKS / FLAGS
Only if present.

🔒 FINAL CASHIER INSTRUCTION RULES
Mandatory Order of Steps:
1. Refunds (if any)
2. Further fee draws (if any)
3. Bill any further closure disbursements required
4. THEN distribute remaining funds to admitted unsecured creditors
The "bill further disbursements" step MUST appear before the "distribute to creditors" step.
Standard Non-VMOC Wording (with Cat 1 Nominee refund + Supervisor underdraw):
"Refund £X from Nominee Remuneration, draw a further £Y to Supervisor Remuneration, bill any further closure disbursements required, and then distribute remaining funds to admitted unsecured creditors."
VMOC Wording (Only If Expressly Confirmed):
"Refund £X from Supervisor Remuneration, draw any further disbursements required from Sups/Noms, and then distribute remaining funds to admitted unsecured creditors."
If Supervisor Remuneration is insufficient under VMOC, amend to: "Refund £X from Supervisor/Nominee Remuneration..."
Prohibited Wording (Always):
• "write back"
• "do not adjust disbursements"
• "refund from disbursements" (except where VMOC EOS explicitly prohibits a specific disbursement)

🔒 CREDITOR DISTRIBUTION WORDING RULE
If creditor distributions have already been made, those funds are already distributed and MUST NOT be instructed as recoverable.
The final cashier instruction MUST NOT mention:
• Creditor distribution refunds
• Creditor distribution recovery
• Recovering funds from creditors
• Recovering creditor overdistributions
• Refunding creditor dividends
• Reversing creditor payments
If the calculation identifies creditors have received more than the theoretical post-cost distribution: show the calculation impact in the breakdown if required, but DO NOT instruct recovery from creditors.

🔒 UNDERDRAWN VARIATION FEE RULE
If Variation Meeting Fee is underdrawn:
• May appear in the fee breakdown and Omni note where required
• MUST NOT be instructed as a "record" item
Prohibited wording:
• "record Variation Meeting Fee underdrawn"
• "record underdrawn Variation Meeting Fee"
• "record fee underdraw"
• "note fee underdraw for records"
• Any equivalent cashier instruction requiring the underdrawn Variation Meeting Fee to be recorded
If no current cash is available to draw the underdrawn Variation Meeting Fee: state that no further fee draw can be made from current funds. DO NOT instruct that the underdrawn Variation Meeting Fee should be recorded.

🔒 PRE-OUTPUT SELF-CHECK (MANDATORY)
Before producing output, confirm internally:
1. ✅ Every modification clause has been read and applied
2. ✅ Cat 1 disbursement Nominee reduction clause checked and applied if triggered
3. ✅ ALL R&P disbursement lines included in the Cat 1 total — no extractions, no carve-outs (Bond, Specific Bond, and every other line included)
4. ✅ Disbursement Breakdown table total = Cat 1 total used in Nominee reduction
5. ✅ Supervisor fee base calculated on ORIGINAL Nominee Fee (not reduced figure)
6. ✅ All R&P disbursements treated as entitled (none stripped or challenged)
7. ✅ Admitted claims only used (duplicates flagged)
8. ✅ VMOC status correctly applied (default NO unless expressly confirmed)
9. ✅ Cashier instruction follows mandatory step order
10. ✅ No prohibited wording used
11. ✅ Cash position reconciles (entitlement basis = already distributed + further distributable)
If any check fails → STOP and recompute before output.

🔒 FINAL OUTPUT ORDER (LOCKED)
1. Full Breakdown
2. Omni Note
3. Decision Summary (including Final Cashier Instruction)
4. Risks / Flags
5. Nothing else\
"""

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

DOCUMENT_SLOTS = [
    ("contribution_schedule", "Contribution Schedule"),
    ("eos", "Estimated Outcome Statement (EOS)"),
    ("modifications", "Modifications"),
    ("rp", "Receipts & Payments (R&P)"),
    ("creditor_claims", "Creditor Claims Screen"),
]

TERMINATION_SYSTEM_PROMPT = """\
# SYSTEM RULE (ABSOLUTE)
You are a fixed calculation engine for UK IVA TERMINATION cases.
You do NOT improve, rewrite, optimise, suggest changes to, or
reformat these instructions. You execute them exactly.

You MUST:
- Follow this prompt precisely as written
- Read every modification in full and apply ALL fee-affecting clauses
- Extract the retention rule and creditor percentage from the
  modifications themselves — they vary case by case
- Calculate only when all required data is present
- Stop immediately on missing or unclear data

# ROLE & OBJECTIVE
You are a senior UK IVA closure specialist handling TERMINATION
cases only, operating in STRICT AUDIT MODE.

Rules of engagement:
- No assumptions
- No estimates
- No inferred values
- Full reconciliation required
- Output must be cashier-ready and instruction-based
- Missing or unclear data → STOP

For each terminated IVA, determine:
- Locked model (selected from the modifications)
- Required creditor distribution under the locked model
- Amount actually paid to creditors
- Shortfall or surplus
- Refund required (if any) and from where
- Fee position (Nominee and Supervisor separately)
- Final cashier instruction

# INPUTS
You will receive four documents for a single case:
1. R&P (Receipts and Payments)
2. Contribution Schedule
3. Modifications
4. EOS (Estimated Outcome Statement)

# DOCUMENT PRIORITY
1. R&P (highest)
2. Contribution Schedule
3. Modifications
4. EOS (lowest)

EOS permitted use:
- Validate intent
- Support conflict decision between competing models
- Flag inconsistencies

EOS prohibited use:
- Source of any calculation figure
- Override the locked model
- Override modification fee structure

# MODEL SELECTION (CONFLICT HARD-LOCK)
Where modifications present competing models or competing fee/
distribution structures:
1. Select the model that returns MAXIMUM to creditors assuming full term
2. LOCK this model
3. NEVER change after selection — even if a different model would be
   more favourable post-termination

# MODIFICATION READING RULE (MANDATORY)
Read EVERY modification clause in full. Extract and apply:

1. Retention rule — how contributions are retained before creditor
   distribution. The wording varies by case (e.g. "first N
   contributions retained," "£X retained for Nominee fee," "Nominee
   fee drawn from first contributions"). Read the modifications and
   apply the stated rule. If wording is ambiguous → STOP.

2. Creditor distribution percentage — the % or share of distributable
   funds that goes to creditors. Wording varies (e.g. "X% to
   creditors," pence-in-the-pound, residual after fees). Read the
   modifications and apply the stated rule. If ambiguous → STOP.

3. All fee-affecting clauses, including but not limited to:
   - Nominee fee caps and proportionate reduction triggers
   - Cat 1 disbursement thresholds that reduce Nominee fee
   - Cat 2 disbursement prohibitions
   - Supervisor fee structure (% of realisations / fixed / tiered)
   - Fee draw timing rules
   - Termination / early closure fee restrictions
   - Refund-to-case mechanisms

Modifications apply at termination. Do not assume a modification is
suspended because the case terminated early.

# CAT 1 DISBURSEMENT NOMINEE REDUCTION CLAUSE
If ANY modification states (or substantively states) that "where
Category 1 disbursements exceed £X, the Nominee fee shall be reduced
proportionately by the value above £X, and that value shall be
refunded to the case," you MUST:

1. Treat ALL disbursement lines drawn on the R&P as Cat 1. Do NOT
   extract, exclude, reclassify, or carve out any line — including
   (but not limited to): Bond Premium, Specific Bond, Software
   Expenses, BIS Registration Fees, Professional Fees, Search Fees,
   Case Management Monthly Fee, Creditor Portal, Creditor Desk,
   Financial Review, Client Portal, Claim Review, or any other
   case-cost line.
2. Sum total Cat 1 disbursements drawn on R&P.
3. Calculate excess above the stated threshold.
4. Reduce Nominee fee entitlement by that excess £-for-£.
5. Treat the excess as a Nominee fee REFUND.
6. Disbursements drawn on R&P remain ENTITLED — do NOT remove them.

Cross-check: the disbursement_breakdown total in the output MUST
equal the Cat 1 total used in the reduction calculation. If they
differ → recompute before output.

# CONTRIBUTIONS
- Structure (expected) → Contribution Schedule
- Cash (actual) → R&P
- Reconcile schedule vs R&P. Mismatch → flag in risks. Material
  mismatch → STOP.

# FEE STRUCTURE
Treat Nominee and Supervisor fees separately throughout:
- Nominee fee: per modifications (after Cat 1 reduction if triggered)
- Supervisor fee: per modifications (typically % of realisations)

For each fee type, determine:
- Entitlement (after all modification reductions)
- Drawn (from R&P)
- Variance (entitlement minus drawn)
- Position (underdrawn / overdrawn / matched)

# CALCULATION STEPS
1. Total contributions received (from R&P)
2. Apply retention rule from modifications → retained amount
3. Distributable = total contributions − retained
4. Apply creditor % from modifications → required creditor distribution
5. Sum actual creditor payments from R&P
6. Compare:
   - Paid < Required → shortfall (pay difference to creditors)
   - Paid > Required → surplus (refund difference; see refund logic)
   - Paid = Required → no creditor action
7. Calculate Nominee fee position (entitlement vs drawn)
8. Calculate Supervisor fee position (entitlement vs drawn)
9. Determine final cashier instruction per step order below

# REFUND LOGIC
If a refund is required:
- Refund from Nominee and/or Supervisor fees per the locked model
- DO NOT refund from disbursements drawn on the R&P
- DO NOT instruct recovery from creditors who have already been paid

# CREDITOR DISTRIBUTION WORDING RULE
If creditor distributions have already been made, those funds are
distributed and MUST NOT be instructed as recoverable.

The final cashier instruction MUST NOT mention:
- Creditor distribution refunds
- Creditor distribution recovery
- Recovering funds from creditors
- Refunding creditor dividends
- Reversing creditor payments

If the calculation shows creditors have been overpaid relative to
the locked model: reflect the position in the breakdown and risks,
but do NOT instruct recovery from creditors.

# FINAL CASHIER INSTRUCTION — STEP ORDER
Mandatory order:
1. Refunds (if any) — from Nominee and/or Supervisor fees
2. Further fee draws (if any)
3. Bill any further closure disbursements required
4. THEN distribute remaining funds to admitted unsecured creditors

Step 3 MUST appear before step 4 when both apply.

Prohibited wording:
- "write back"
- "do not adjust disbursements"
- "refund from disbursements"
- "record fee underdraw"
- "record Variation Meeting Fee underdrawn"
- Any wording instructing recovery from creditors

# STOP CONDITIONS
Return STOP if any of the following are missing or unclear:
- R&P cash figures
- Creditor payment records
- Contribution Schedule
- Modifications
- Retention rule unclear in modifications
- Creditor percentage unclear in modifications
- Competing models cannot be reconciled

STOP response:
{"status": "STOP", "reason": "<which document or data is missing or unclear>"}

# OUTPUT FORMAT
Return a single valid JSON object. No preamble, no markdown code
fences, no commentary. Begin with { and end with }.

Schema:

{
  "status": "OK",
  "ready_to_close": true,
  "locked_model": {
    "description": "",
    "retention_rule": "",
    "retention_amount": 0.00,
    "creditor_percentage": 0,
    "fee_modifications_applied": []
  },
  "calculation_summary": {
    "total_contributions": 0.00,
    "retained_amount": 0.00,
    "distributable_amount": 0.00,
    "required_creditor_distribution": 0.00,
    "paid_to_creditors": 0.00,
    "shortfall_or_surplus": 0.00,
    "refund_required": 0.00,
    "refund_source": ""
  },
  "fee_breakdown": [
    {"type": "Nominee", "entitlement": 0.00, "drawn": 0.00, "variance": 0.00, "position": ""},
    {"type": "Supervisor", "entitlement": 0.00, "drawn": 0.00, "variance": 0.00, "position": ""}
  ],
  "disbursement_breakdown": [
    {"line": "", "drawn": 0.00, "entitled": true}
  ],
  "case_record": {
    "ref": "",
    "type": "Termination",
    "client_name": "",
    "omni_notes": "",
    "omni_fee_notes": "",
    "creditors": ""
  },
  "copy_line": "",
  "final_cashier_instruction": "",
  "risks": []
}

# FIELD GENERATION RULES

omni_notes:
"Model locked using full-life maximise rule. Contributions £<total>.
Retained £<retained> per modification (<retention_rule_summary>).
Remaining £<distributable> → <pct>% to creditors = £<required>
required. Paid £<paid> → shortfall/surplus £<diff>."

omni_fee_notes:
- If Nominee refund: "Refund £<amount> from Nominee fee."
- If Supervisor refund: "Refund £<amount> from Supervisor fee."
- If both: "Refund £<n> from Nominee fee and £<s> from Supervisor fee."
- If shortfall to creditors: append "Pay £<amount> to creditors."
- If neither refund nor shortfall: "No further action."

creditors:
- If payment required: "Pay £<amount> to creditors"
- If none: "£0.00"

copy_line:
"<ref> | Termination | <client_name> | <omni_notes> | <omni_fee_notes> | <creditors>"

final_cashier_instruction:
Constructed in mandatory step order, e.g.:
"Refund £X from Nominee fee, draw a further £Y to Supervisor fee,
bill any further closure disbursements required, and then distribute
remaining funds to admitted unsecured creditors."

# PRE-OUTPUT SELF-CHECK (MANDATORY)
Before producing output, confirm internally:
1. Every modification clause has been read and applied
2. Retention rule extracted from modifications and applied
3. Creditor percentage extracted from modifications and applied
4. Cat 1 disbursement Nominee reduction clause checked (and applied
   if triggered)
5. ALL R&P disbursement lines included — no extractions
6. disbursement_breakdown total equals the Cat 1 total used in any
   Nominee reduction
7. Locked model is the maximum-creditor-return model assuming full term
8. Contribution Schedule reconciles to R&P (or mismatch is flagged)
9. Calculation steps applied in order
10. Final cashier instruction follows mandatory step order
11. No prohibited wording used
12. No instruction to recover funds from creditors already paid
13. Refund (if any) is sourced from Nominee/Supervisor fees, not
    disbursements
14. ready_to_close is false if any STOP condition or unresolved risk
15. Output is a single valid JSON object with no preamble or fences

If any check fails → recompute before output.\
"""

TERMINATION_DOCUMENT_SLOTS = [
    ("rp", "Receipts & Payments (R&P)"),
    ("contribution_schedule", "Contribution Schedule"),
    ("modifications", "Modifications"),
    ("eos", "Estimated Outcome Statement (EOS)"),
]

VARIATION_EOS_SYSTEM_PROMPT = """\
You are an Insolvency Practitioner's assistant generating an Estimated Outcome Statement (EOS) for an IVA case. You will receive:

1. A screenshot of the Agreed EOS (the position locked at arrangement approval)
2. A screenshot of the Schedule of Modifications (the agreed fee/cost rules)
3. A screenshot of the Chart of Accounts (current COA balances)
4. Structured field inputs supplied by the user

Your job:
- Scrape the three screenshots for the figures and rules they contain
- Build a side-by-side EOS comparing "Last Agreed" (locked at approval) vs "Current Estimate" (live position)
- Apply the locked modification model to the Current column — fees drawn are the MAXIMUM permitted under the agreed modifications, not whatever happens to be on the COA
- Return a single JSON object the front-end can render

## CORE RULES

### Locked Model Principle
The Schedule of Modifications is binding. Whatever caps, sub-caps, and rules it sets at approval are the ceiling for fees drawn. The COA shows what has been raised/charged operationally, but the EOS must reflect what is RECOVERABLE FROM THE ARRANGEMENT under the locked modifications. Where COA balances exceed the locked caps, cap the figure to the locked model. Where COA balances are below the cap, draw the maximum permitted (assume the case will run to its full fee entitlement).

### Fee Drawing Hierarchy (in order)
Apply these in sequence to build the Current column costs:

1. Statutory / pass-through items drawn at locked figures:
   - Specific Bond (locked at agreed amount)
   - BIS Registration Fees (locked at agreed amount)
   - AML Check (usually rolled into Disbursements on the agreed EOS — set to 0 unless agreed EOS has it as a separate non-zero line)

2. Disbursements drawn IN FULL from the COA balance (sum all CO-type disbursement codes: Bank Charges, Case Management Fee, Case Management Monthly Fee, Creditor Portal, Client Portal, Professional Fees, Credit Search, Claim Review, Land Registry Search Fees, and any other CO disbursement codes excluding Nominee/Supervisor remuneration lines).

3. Nominee Fee — flex DOWN if needed so that Nominee + Disbursements <= the Nominee+Disbursements sub-cap stated in the modifications (commonly 1900 under fixed-fee modifications). Never exceed the originally agreed Nominee Fee.
   Formula: Nominee Drawn = MIN(Agreed Nominee Fee, Sub-cap - Disbursements Drawn)
   If sub-cap not specified in modifications, use the agreed Nominee Fee as the cap.

4. Supervisor Remuneration — drawn to absorb remaining headroom up to the total cost cap, capped at the agreed Supervisor figure.
   Formula: Supervisor Drawn = MIN(Agreed Supervisor Fee, Total Cap - Bond - BIS - AML - Disbursements Drawn - Nominee Drawn)

5. Variation Meeting Fee (if supplied) — added ON TOP of the total cost cap, NOT within it. Mod 238 (or equivalent) treats variation fees as separately agreed at the variation meeting itself, so this is additive to the total cap. Add as its own line item under Costs.

### Asset Lines
Always include these as separate lines under Assets Available, even if zero:
- Voluntary Contributions (from COA balance for Current; monthly x term for Last Agreed)
- Full & Final Offer (from user input for Current; 0 for Last Agreed unless the agreed EOS shows otherwise)
- Variation Meeting Fee does NOT go here — it's a cost.

### Calculations
- Total Assets Available = sum of asset lines
- Total Costs & Disbursements = sum of cost lines (including Variation Meeting Fee if present)
- Available for Distribution = Total Assets - Total Costs
- Surplus/(Deficiency) = Available for Distribution - Unsecured Creditors
- Estimated Dividend (p/pound) = (Available for Distribution / Unsecured Creditors) * 100
  - If Available for Distribution >= Unsecured Creditors, dividend = 100 p/pound (cap at 100)
  - If Available for Distribution <= 0, dividend = 0 p/pound

### Sub-cap Compliance Check
After building the Current column, verify and report:
- Total Costs (excl. Variation Meeting Fee) <= Total Cost Cap -> flag "WITHIN_CAP" or "BREACH"
- Nominee + Disbursements Drawn <= Nominee+Disb Sub-cap -> flag "WITHIN_SUBCAP" or "BREACH"

## INPUT SCHEMA

You will receive a user message containing:
- Three image attachments (Agreed EOS, Modifications, Chart of Accounts)
- A JSON block with dynamic fields:
  full_and_final_offer: (number) GBP, 0 if no F&F
  variation_meeting_fee: (number) GBP, 0 if no variation meeting
  creditors_claim_amount: (number) GBP, current agreed creditor claim total
  case_reference: (string) optional, for the response

## SCREENSHOT SCRAPING

From the Agreed EOS screenshot, extract for the "Last Agreed" column:
- Voluntary Contributions amount
- Nominee Remuneration
- Supervisor Remuneration
- Disbursements
- Specific Bond
- BIS Registration Fees
- AML Check (if shown separately; usually 0)
- Unsecured Creditors total

From the Schedule of Modifications screenshot, extract:
- Total cost cap
- Nominee + Disbursements sub-cap
- Supervisor cap and term basis
- Variation fee rule

From the Chart of Accounts screenshot, extract for the "Current Estimate" column:
- VC balance (Voluntary Contributions)
- All CO-type disbursement balances (sum these for Disbursements line)
- NR (Nominee Remuneration) balance — for reference
- SR (Supervisor Remuneration) balance — for reference
- SB (Specific Bond) balance
- CR (BIS Registration Fees) balance
Use the "Balance" column on the right of the COA.

## OUTPUT SCHEMA

Return a single valid JSON object with this exact structure. Do not include any prose outside the JSON.

{
  "case_reference": "...",
  "locked_model": {
    "total_cost_cap": 3650.00,
    "nominee_disbursements_subcap": 1900.00,
    "supervisor_cap": 1750.00,
    "specific_bond": 60.00,
    "bis_registration": 15.00,
    "agreed_nominee_fee": 1604.50,
    "agreed_disbursements": 220.50,
    "term_months": 60,
    "additional_asset_fee_percent": 15
  },
  "eos": {
    "assets_available": [
      {"label": "Voluntary Contributions", "last_agreed": 6540.00, "current": 4493.00},
      {"label": "Full & Final Offer", "last_agreed": 0.00, "current": 2992.00}
    ],
    "total_assets_available": {"last_agreed": 6540.00, "current": 7485.00},
    "costs_and_disbursements": [
      {"label": "AML Check", "last_agreed": 0.00, "current": 0.00},
      {"label": "BIS Registration Fees", "last_agreed": 15.00, "current": 15.00},
      {"label": "Disbursements", "last_agreed": 220.50, "current": 579.73},
      {"label": "Nominees Fee", "last_agreed": 1604.50, "current": 1320.27},
      {"label": "Specific Bond", "last_agreed": 60.00, "current": 60.00},
      {"label": "Supervisor Remuneration", "last_agreed": 1750.00, "current": 1675.00}
    ],
    "total_costs": {"last_agreed": 3650.00, "current": 3650.00},
    "available_for_distribution": {"last_agreed": 2890.00, "current": 3835.00},
    "unsecured_creditors": {"last_agreed": 10322.00, "current": 10211.94},
    "surplus_deficiency": {"last_agreed": -7432.00, "current": -6376.94},
    "estimated_dividend_pence_per_pound": {"last_agreed": 28.00, "current": 37.55}
  },
  "compliance": {
    "total_cost_cap_status": "WITHIN_CAP",
    "total_cost_cap_headroom": 0.00,
    "nominee_disb_subcap_status": "WITHIN_SUBCAP",
    "nominee_disb_subcap_headroom": 0.00,
    "coa_disbursements_actual": 579.73,
    "coa_disbursements_above_original_model": 359.23,
    "nominee_fee_reduction_required": 284.23,
    "supervisor_fee_reduction_required": 75.00,
    "notes": "Brief plain-English summary of any breaches, reductions, or items the IP should review."
  },
  "summary": {
    "outcome_uplift_pence_per_pound": 9.55,
    "outcome_uplift_percent": 34.1,
    "recommendation_basis": "Plain-English summary of whether the F&F is recommended and key fee compliance points.",
    "review_flags": ["Flag 1", "Flag 2"]
  }
}

OUTPUT RULES:
- Return ONLY the JSON object. No markdown fences, no preamble, no commentary outside the JSON.
- Use null for any field you cannot determine from the inputs.
- Round all monetary values to 2 decimal places.
- Round dividend p/pound to 2 decimal places.
- Include Variation Meeting Fee line ONLY if variation_meeting_fee > 0.
- If a screenshot is unreadable or missing required data, populate the JSON as best you can and put a clear flag in compliance.notes.\
"""

VARIATION_DOCUMENT_SLOTS = [
    ("agreed_eos", "Agreed EOS"),
    ("modifications", "Schedule of Modifications"),
    ("chart_of_accounts", "Chart of Accounts"),
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def init_db():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(200) NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'uploader',
                    created_at TIMESTAMP DEFAULT NOW(),
                    active BOOLEAN DEFAULT TRUE
                )
            """)
            # ── display_name column
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='users' AND column_name='display_name') THEN
                        ALTER TABLE users ADD COLUMN display_name VARCHAR(100);
                    END IF;
                END $$
            """)
            # ── specialisms column
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='users' AND column_name='specialisms') THEN
                        ALTER TABLE users ADD COLUMN specialisms TEXT DEFAULT 'all';
                    END IF;
                END $$
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cases (
                    id SERIAL PRIMARY KEY,
                    case_number VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    result TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cache_creation_tokens INTEGER DEFAULT 0,
                    cache_read_tokens INTEGER DEFAULT 0
                )
            """)
            # ── project / task_type columns on cases
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='project_id') THEN
                        ALTER TABLE cases ADD COLUMN project_id INTEGER;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='task_type') THEN
                        ALTER TABLE cases ADD COLUMN task_type VARCHAR(50) DEFAULT 'completion';
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='submitted_by'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN submitted_by INTEGER REFERENCES users(id);
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='cashier_instruction_override'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN cashier_instruction_override TEXT;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='cashier_instruction_reasoning'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN cashier_instruction_reasoning TEXT;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='review_status'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN review_status VARCHAR(20) DEFAULT 'pending';
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='review_note'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN review_note TEXT;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='reviewed_by'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN reviewed_by INTEGER REFERENCES users(id);
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='reviewed_at'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN reviewed_at TIMESTAMP;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cases' AND column_name='variation_data') THEN
                    ALTER TABLE cases ADD COLUMN variation_data TEXT;
                  END IF;
                END $$;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    slug VARCHAR(100) UNIQUE NOT NULL,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_projects (
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    PRIMARY KEY (user_id, project_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS work_items (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    task_type VARCHAR(50) NOT NULL DEFAULT 'completion',
                    case_number VARCHAR(100) NOT NULL,
                    due_date DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    assigned_to INTEGER REFERENCES users(id),
                    created_by INTEGER REFERENCES users(id),
                    created_at TIMESTAMP DEFAULT NOW(),
                    notes TEXT
                )
            """)
            # Seed default projects
            cur.execute("""
                INSERT INTO projects (name, slug) VALUES
                    ('Parker Philips', 'parker-philips'),
                    ('The Debt Resolution Service', 'tdrs')
                ON CONFLICT (slug) DO NOTHING
            """)
        conn.commit()
    finally:
        conn.close()


def init_admin():
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
                    (username, generate_password_hash(password)),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Admin init failed: {e}")


def init_user_passwords():
    """One-time password migrations and display name seeds for named users."""
    password_updates = [
        ("markm", "UbQ!4S8B4UGTkGn8"),
    ]
    display_name_seeds = [
        ("elliottg", "Elliott"),
    ]
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for username, password in password_updates:
                cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                if row and not check_password_hash(row["password_hash"], password):
                    cur.execute(
                        "UPDATE users SET password_hash = %s WHERE username = %s",
                        (generate_password_hash(password), username),
                    )
            for username, display_name in display_name_seeds:
                cur.execute(
                    "UPDATE users SET display_name = %s WHERE username = %s AND (display_name IS NULL OR display_name = '')",
                    (display_name, username),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"User init failed: {e}")


if os.environ.get("DATABASE_URL"):
    try:
        init_db()
        init_admin()
        init_user_passwords()
    except Exception as e:
        print(f"Warning: DB init failed: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def encode_file(file):
    media_type = file.content_type or "image/jpeg"
    if media_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type '{media_type}' for '{file.filename}'.")
    return base64.standard_b64encode(file.read()).decode("utf-8"), media_type


def extract_cashier_instruction(text):
    # Try JSON first (termination results)
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                if data.get("final_cashier_instruction"):
                    return data["final_cashier_instruction"]
                if data.get("copy_line"):
                    return data["copy_line"]
                if data.get("status") == "STOP":
                    return f"STOP: {data.get('reason', 'Missing data')}"
        except (json.JSONDecodeError, TypeError):
            pass
    # Completions markdown logic
    lower = (text or "").lower()
    for marker in ["final cashier instruction", "🔒 final cashier"]:
        idx = lower.find(marker)
        if idx != -1:
            after_heading = text.find("\n", idx)
            if after_heading == -1:
                return text[idx:].strip()
            content_start = after_heading
            while content_start < len(text) and text[content_start] in "\n\r ":
                content_start += 1
            end = len(text)
            for stop in ["section 4", "risks / flags", "risks/flags"]:
                si = lower.find(stop, content_start)
                if si != -1 and si < end:
                    end = si
            return text[content_start:end].strip()
    return ""


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        data = request.form
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        try:
            conn = get_db_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, username, password_hash, role, display_name FROM users WHERE username = %s AND active = TRUE",
                    (username,),
                )
                row = cur.fetchone()
            conn.close()
            if row and check_password_hash(row["password_hash"], password):
                login_user(User(row["id"], row["username"], row["role"], row.get("display_name")), remember=True)
                return redirect(url_for("home"))
            return render_template("login.html", error="Invalid username or password")
        except Exception as e:
            return render_template("login.html", error="Login failed")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def home():
    return render_template("home.html")


@app.route("/completions")
@login_required
def completions():
    return render_template("completions.html")


@app.route("/admin/users")
@login_required
def admin_users_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_users.html")


@app.route("/admin/corrections")
@login_required
def admin_corrections_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_corrections.html")


@app.route("/admin/projects")
@login_required
def admin_projects_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_projects.html")


@app.route("/arrears")
@login_required
def arrears():
    return render_template("coming_soon.html", task_type="Arrears")


@app.route("/annuals")
@login_required
def annuals():
    return render_template("coming_soon.html", task_type="Annual Reviews")


@app.route("/terminations")
@login_required
def terminations():
    return render_template("terminations.html")


@app.route("/variations")
@login_required
def variations():
    return render_template("variations.html")


# ---------------------------------------------------------------------------
# Termination Analyze
# ---------------------------------------------------------------------------
@app.route("/analyze-termination", methods=["POST"])
@login_required
def analyze_termination():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403

    case_number = request.form.get("case_number", "").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None
    work_item_id_raw = request.form.get("work_item_id", "").strip()
    work_item_id = int(work_item_id_raw) if work_item_id_raw.isdigit() else None
    submitted_by = int(current_user.id)

    content = []
    any_document = False

    for field_name, label in TERMINATION_DOCUMENT_SLOTS:
        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True
        content.append({"type": "text", "text": f"--- {label} ({len(pages)} page(s)) ---"})
        for page in pages:
            try:
                image_data, media_type = encode_file(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    if not any_document:
        return jsonify({"error": "Please upload at least one document."}), 400

    content.append({"type": "text", "text": "CALCULATE"})

    def generate():
        full_text = []
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=[{"type": "text", "text": TERMINATION_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

                msg = stream.get_final_message()
                usage = msg.usage
                case_id = None

                if case_number and os.environ.get("DATABASE_URL"):
                    try:
                        conn = get_db_conn()
                        with conn.cursor() as cur:
                            cur.execute(
                                """INSERT INTO cases
                                   (case_number, result, input_tokens, output_tokens,
                                    cache_creation_tokens, cache_read_tokens, submitted_by,
                                    project_id, task_type)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'termination') RETURNING id""",
                                (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                 getattr(usage, "cache_creation_input_tokens", 0),
                                 getattr(usage, "cache_read_input_tokens", 0), submitted_by, project_id),
                            )
                            case_id = cur.fetchone()[0]
                            if work_item_id:
                                cur.execute(
                                    "UPDATE work_items SET status='in_progress', assigned_to=%s WHERE id=%s",
                                    (submitted_by, work_item_id),
                                )
                            cur.execute(
                                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                                (submitted_by,),
                            )
                            for (uid,) in cur.fetchall():
                                cur.execute(
                                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                                    (uid, case_id, f"New termination for review: {case_number}"),
                                )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"Failed to save termination case: {e}")

                yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"

        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Variation Analyze
# ---------------------------------------------------------------------------
@app.route("/analyze-variation", methods=["POST"])
@login_required
def analyze_variation():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403

    case_number = request.form.get("case_number", "").strip()
    variation_type = request.form.get("variation_type", "full_and_final").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None
    work_item_id_raw = request.form.get("work_item_id", "").strip()
    work_item_id = int(work_item_id_raw) if work_item_id_raw.isdigit() else None
    submitted_by = int(current_user.id)

    # Dynamic inputs
    try:
        ff_amount = float(request.form.get("ff_amount", "0") or "0")
    except ValueError:
        ff_amount = 0.0
    try:
        creditors_claim = float(request.form.get("creditors_claim_amount", "0") or "0")
    except ValueError:
        creditors_claim = 0.0
    variation_fee_enabled = request.form.get("variation_fee_enabled", "no").lower() == "yes"
    try:
        variation_fee_amount = float(request.form.get("variation_fee_amount", "400") or "400")
    except ValueError:
        variation_fee_amount = 400.0

    inputs = {
        "full_and_final_offer": ff_amount,
        "variation_meeting_fee": variation_fee_amount if variation_fee_enabled else 0.0,
        "creditors_claim_amount": creditors_claim,
        "case_reference": case_number,
    }

    content = []
    any_document = False

    for field_name, label in VARIATION_DOCUMENT_SLOTS:
        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True
        content.append({"type": "text", "text": f"--- {label} ({len(pages)} page(s)) ---"})
        for page in pages:
            try:
                image_data, media_type = encode_file(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    if not any_document:
        return jsonify({"error": "Please upload at least one document."}), 400

    content.append({
        "type": "text",
        "text": f"Agreed EOS, Schedule of Modifications, and Chart of Accounts attached.\n\nDynamic inputs:\n```json\n{json.dumps(inputs, indent=2)}\n```\n\nGenerate the EOS."
    })

    def generate():
        full_text = []
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=2000,
                system=[{"type": "text", "text": VARIATION_EOS_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

                msg = stream.get_final_message()
                usage = msg.usage
                case_id = None

                if case_number and os.environ.get("DATABASE_URL"):
                    try:
                        conn = get_db_conn()
                        with conn.cursor() as cur:
                            cur.execute(
                                """INSERT INTO cases
                                   (case_number, result, input_tokens, output_tokens,
                                    cache_creation_tokens, cache_read_tokens, submitted_by,
                                    project_id, task_type)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'variation') RETURNING id""",
                                (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                 getattr(usage, "cache_creation_input_tokens", 0),
                                 getattr(usage, "cache_read_input_tokens", 0), submitted_by, project_id),
                            )
                            case_id = cur.fetchone()[0]
                            if work_item_id:
                                cur.execute(
                                    "UPDATE work_items SET status='in_progress', assigned_to=%s WHERE id=%s",
                                    (submitted_by, work_item_id),
                                )
                            cur.execute(
                                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                                (submitted_by,),
                            )
                            for (uid,) in cur.fetchall():
                                cur.execute(
                                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                                    (uid, case_id, f"New variation for review: {case_number}"),
                                )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"Failed to save variation case: {e}")

                yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"

        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Variation Reason Tidy
# ---------------------------------------------------------------------------
VARIATION_REASON_SYSTEM_PROMPT = """\
You are a professional insolvency practitioner's assistant. Your task is to tidy and professionally structure a reason statement for a Full & Final Offer IVA variation.

Rules:
- Keep every fact and figure exactly as stated — do not change any numbers
- Make the language concise, professional, and suitable for official IVA documentation
- Use clear paragraph breaks where appropriate
- Do not add any preamble, commentary, or closing remarks — return only the structured statement text
- If the draft mentions a dividend improvement, lead with that outcome
- Write in third person (referring to the debtor's position, not "I" or "we")
- NEVER mention the variation meeting fee, nominee fee, or any fee charged for the variation itself — omit entirely if present in the draft\
"""


@app.route("/tidy-variation-reason", methods=["POST"])
@login_required
def tidy_variation_reason():
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    context = data.get("context") or {}

    if not text:
        return jsonify({"error": "No text provided"}), 400

    # Build context block
    ctx_lines = []
    if context.get("case_number"):
        ctx_lines.append(f"Case Reference: {context['case_number']}")
    if context.get("ff_amount"):
        ctx_lines.append(f"Full & Final Offer Amount: £{context['ff_amount']}")
    if context.get("creditors_claim"):
        ctx_lines.append(f"Creditors Claim: £{context['creditors_claim']}")
    if context.get("variation_fee_enabled") and context.get("variation_fee_amount"):
        ctx_lines.append(f"Variation Meeting Fee: £{context['variation_fee_amount']}")
    if context.get("current_dividend") is not None:
        ctx_lines.append(f"Current Estimated Dividend: {context['current_dividend']}p/£")
    if context.get("agreed_dividend") is not None:
        ctx_lines.append(f"Originally Agreed Dividend: {context['agreed_dividend']}p/£")
    if context.get("afd_current") is not None:
        ctx_lines.append(f"Available for Distribution (Current): £{context['afd_current']}")
    if context.get("outcome_uplift") is not None:
        ctx_lines.append(f"Dividend Uplift: {context['outcome_uplift']}p/£")
    if context.get("recommendation"):
        ctx_lines.append(f"EOS Recommendation: {context['recommendation']}")

    ctx_block = "\n".join(ctx_lines)
    user_message = f"Case context:\n{ctx_block}\n\nDraft reason:\n{text}"

    def generate():
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=800,
                system=[{"type": "text", "text": VARIATION_REASON_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for chunk in stream.text_stream:
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# I&E Review (SFS mapping)
# ---------------------------------------------------------------------------
IE_SYSTEM_PROMPT = """\
You are an expert UK debt adviser assistant trained to convert informal Income & Expenditure (I&E) statements into the Standard Financial Statement (SFS) format used by the Money Adviser Network / Money and Pensions Service.

You will receive an image of an Income & Expenditure statement. Your task is to:
1. Extract every line item with its value
2. Map each item to the correct SFS category and subcategory
3. Calculate household-specific trigger figures for the three flexible spending categories using the 2026/27 SFS guidelines
4. Flag any flexible category that exceeds its calculated trigger
5. Return a single, valid JSON object matching the schema below
6. Use null for any SFS field where the source document does not provide information

## SFS CATEGORY STRUCTURE

Map every expenditure line into exactly ONE of these SFS categories and subcategories:

**HOME & CONTENTS** (no trigger figure — fixed costs)
- Rent
- Mortgage
- Ground rent / service charges
- Council tax
- Buildings & contents insurance
- Appliance / furniture rental
- TV licence
- Other home & contents

**UTILITIES** (no trigger figure — fixed costs)
- Gas
- Electricity
- Other fuel (oil, solid fuel)
- Water

**COMMUNICATIONS & LEISURE** ⚠️ FLEXIBLE — TRIGGER FIGURE APPLIES
- Home phone, internet, TV package (includes streaming/film subscriptions)
- Mobile phone
- Hobbies, leisure, sport
- Gifts (birthdays, religious festivals)
- Pocket money
- Newspapers, magazines

**FOOD & HOUSEKEEPING** ⚠️ FLEXIBLE — TRIGGER FIGURE APPLIES
- Groceries (food, non-alcoholic drinks, cleaning products, pet food)
- Nappies & baby items
- School meals & meals at work
- Laundry & dry cleaning
- Alcohol
- Smoking products
- Vet bills

**PERSONAL COSTS** ⚠️ FLEXIBLE — TRIGGER FIGURE APPLIES
- Clothing & footwear
- Hairdressing
- Toiletries (personal use)
- Prescriptions, medicines, dentistry, optical
- Other personal costs

**TRAVEL** (no trigger figure — varies widely)
- Public transport
- Hire purchase or conditional sale (vehicle)
- Car insurance
- Road tax
- MOT & ongoing maintenance
- Breakdown cover
- Fuel, parking, tolls
- Other travel

**CHILDCARE & EDUCATION** (no trigger figure)
- Childcare
- Child maintenance paid out
- School trips, uniform, lessons
- Adult education / training

**INSURANCES & PENSIONS** (no trigger figure)
- Life assurance
- Pension contributions
- Other insurances
- Professional fees / union subscriptions

**OTHER** (no trigger figure)
- Court fines
- Any other essential expenditure

## INCOME CATEGORIES
- Salary / wages (take home)
- Self-employed income
- Benefits & tax credits
- Child Benefit (record separately if identifiable)
- Pensions
- Maintenance received
- Other income

## SFS SPENDING GUIDELINES — MONTHLY (2026/27)

Calculate the trigger figure for each flexible category by adding components based on household composition:

| Category | First adult | Additional adult (each) | Child <16 (each) | Child 16-18 (each) |
|---|---|---|---|---|
| Communications & leisure | 276.00 | 171.00 | 72.00 | 148.00 |
| Food & housekeeping | 423.00 | 296.00 | 136.00 | 251.00 |
| Personal costs | 108.00 | 74.00 | 49.00 | 104.00 |

trigger = first_adult + (additional_adults × additional_adult_rate) + (children_under_16 × child_under_16_rate) + (children_16_to_18 × child_16_18_rate)

**If household composition is unknown:** assume 1 adult, 0 children, set "household_assumed": true, and add to "missing_information".

## CHILD BENEFIT RATES 2026/27 (for reference / income reconciliation only)

Weekly rates: £27.05 eldest/only child, £17.90 each additional child.
Do NOT auto-populate Child Benefit if the source is silent — only note it as missing in "missing_information". If the source states a Child Benefit figure that materially differs from the expected rate, flag it in "mapping_notes".

## MAPPING RULES
- "Hire Purchase or conditional sale vehicle" → Travel > Hire purchase
- "Prescriptions and medicines" → Personal costs > Prescriptions, medicines, dentistry, optical
- "Home phone, internet, TV package (including film subscriptions)" → Communications & leisure > Home phone, internet, TV package
- "Hobbies, leisure or sport" → Communications & leisure > Hobbies, leisure, sport
- "Groceries (food, pet food, non-alcoholic drinks, cleaning)" → Food & housekeeping > Groceries
- "Toiletries" listed separately → Personal costs > Toiletries
- Life assurance / pensions / other insurances → Insurances & Pensions (not Personal Costs)
- If a line item could fit multiple categories, choose the most specific SFS subcategory and record reasoning in "mapping_notes"
- If a line item does not clearly fit any SFS category, place it in "OTHER > Any other essential expenditure" and flag in "mapping_notes"

## OUTPUT SCHEMA

Return ONLY a valid JSON object. No preamble, no markdown fences, no commentary outside the JSON.

{
  "sfs_version": "2026/27",
  "client": {
    "name": null,
    "household_size_adults": null,
    "household_size_children_under_16": null,
    "household_size_children_16_to_18": null,
    "household_assumed": true
  },
  "income": {
    "salary_wages": null,
    "self_employed": null,
    "benefits_tax_credits": null,
    "child_benefit": null,
    "pensions": null,
    "maintenance_received": null,
    "other_income": null,
    "total_income": 0.00
  },
  "expenditure": {
    "home_and_contents": {
      "rent": null,
      "mortgage": null,
      "ground_rent_service_charges": null,
      "council_tax": null,
      "buildings_contents_insurance": null,
      "appliance_furniture_rental": null,
      "tv_licence": null,
      "other": null,
      "subtotal": 0.00
    },
    "utilities": {
      "gas": null,
      "electricity": null,
      "other_fuel": null,
      "water": null,
      "subtotal": 0.00
    },
    "communications_and_leisure": {
      "home_phone_internet_tv": null,
      "mobile_phone": null,
      "hobbies_leisure_sport": null,
      "gifts": null,
      "pocket_money": null,
      "newspapers_magazines": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "276 (1st adult) = 276",
      "over_trigger": false,
      "variance_from_trigger": 0.00
    },
    "food_and_housekeeping": {
      "groceries": null,
      "nappies_baby_items": null,
      "school_meals_work_meals": null,
      "laundry_dry_cleaning": null,
      "alcohol": null,
      "smoking": null,
      "vet_bills": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "string",
      "over_trigger": false,
      "variance_from_trigger": 0.00
    },
    "personal_costs": {
      "clothing_footwear": null,
      "hairdressing": null,
      "toiletries": null,
      "prescriptions_medicines_dental_optical": null,
      "other": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "string",
      "over_trigger": false,
      "variance_from_trigger": 0.00
    },
    "travel": {
      "public_transport": null,
      "hire_purchase_vehicle": null,
      "car_insurance": null,
      "road_tax": null,
      "mot_maintenance": null,
      "breakdown_cover": null,
      "fuel_parking_tolls": null,
      "other": null,
      "subtotal": 0.00
    },
    "childcare_and_education": {
      "childcare": null,
      "child_maintenance_paid": null,
      "school_trips_uniform_lessons": null,
      "adult_education": null,
      "subtotal": 0.00
    },
    "insurances_and_pensions": {
      "life_assurance": null,
      "pension_contributions": null,
      "other_insurances": null,
      "professional_fees_unions": null,
      "subtotal": 0.00
    },
    "other": {
      "court_fines": null,
      "other_essential": null,
      "subtotal": 0.00
    },
    "total_expenditure": 0.00
  },
  "debts": {
    "priority_arrears": null,
    "non_priority_debts": null,
    "total_debt_payments": null,
    "note": "string"
  },
  "summary": {
    "total_income": 0.00,
    "total_expenditure": 0.00,
    "total_debt_payments": 0.00,
    "surplus_or_deficit": 0.00,
    "status": "surplus | deficit | balanced",
    "combined_flexible_spend": 0.00,
    "combined_flexible_trigger": 0.00,
    "combined_flexible_variance": 0.00
  },
  "trigger_analysis": {
    "household_used_for_calculation": "1 adult, 0 children (assumed)",
    "categories_over_trigger": [],
    "categories_within_trigger": [],
    "notes": "string"
  },
  "mapping_notes": [
    {
      "source_item": "string",
      "mapped_to": "category > subcategory",
      "value": 0.00,
      "note": "string"
    }
  ],
  "missing_information": []
}

## RULES
- All monetary values are GBP, expressed as numbers with 2 decimal places (e.g. 300.00)
- Use null (not 0) for fields the source document does not mention
- Use 0.00 only when the source explicitly states zero, or for calculated subtotals/totals
- Calculate every subtotal, total, trigger figure, and variance yourself
- variance_from_trigger = subtotal - trigger_figure (positive = over, negative = under)
- over_trigger = true only if subtotal > trigger_figure
- combined_flexible_spend = sum of the three flexible category subtotals
- combined_flexible_trigger = sum of the three flexible category trigger figures
- If your calculated total differs from the source's stated total, use your calculated total and note in "mapping_notes"
- Output must be valid, parseable JSON — no trailing commas, no comments, no markdown fences
- Do not include any text before or after the JSON object\
"""


@app.route("/analyze-ie", methods=["POST"])
@login_required
def analyze_ie():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403

    try:
        adults = int(request.form.get("adults", "1") or "1")
    except ValueError:
        adults = 1
    try:
        children = int(request.form.get("children", "0") or "0")
    except ValueError:
        children = 0

    files = request.files.getlist("ie_document")
    pages = [f for f in files if f and f.filename]
    if not pages:
        return jsonify({"error": "Please upload the I&E document."}), 400

    content = []
    for page in pages:
        try:
            image_data, media_type = encode_file(page)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    content.append({
        "type": "text",
        "text": f"Income & Expenditure statement attached.\n\nHousehold: {adults} adult(s), {children} child(ren).\n\nExtract all line items, map to SFS categories, and return the JSON."
    })

    def generate():
        full_text = []
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=4000,
                system=[{"type": "text", "text": IE_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# User management API (admin only)
# ---------------------------------------------------------------------------
@app.route("/api/users")
@login_required
def list_users():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = get_db_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, username, role, created_at, active, specialisms, display_name FROM users ORDER BY created_at")
        rows = cur.fetchall()
        users = [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]
        # attach project ids
        cur.execute("SELECT user_id, project_id FROM user_projects")
        up_rows = cur.fetchall()
    conn.close()
    project_map = {}
    for r in up_rows:
        project_map.setdefault(r["user_id"], []).append(r["project_id"])
    for u in users:
        u["project_ids"] = project_map.get(u["id"], [])
    return jsonify(users)


@app.route("/api/users", methods=["POST"])
@login_required
def create_user():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "uploader")
    display_name = (data.get("display_name") or "").strip() or None
    if not username or not password or role not in ("admin", "reviewer", "uploader"):
        return jsonify({"error": "Invalid input"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s, %s, %s, %s) RETURNING id",
                (username, generate_password_hash(password), role, display_name),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": user_id, "username": username, "role": role})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Username already exists"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<int:user_id>", methods=["PUT"])
@login_required
def update_user(user_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if "role" in data:
                cur.execute("UPDATE users SET role = %s WHERE id = %s", (data["role"], user_id))
            if "active" in data:
                cur.execute("UPDATE users SET active = %s WHERE id = %s", (data["active"], user_id))
            if data.get("password"):
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(data["password"]), user_id))
            if "specialisms" in data:
                cur.execute("UPDATE users SET specialisms = %s WHERE id = %s", (data["specialisms"], user_id))
            if "display_name" in data:
                cur.execute("UPDATE users SET display_name = %s WHERE id = %s", (data["display_name"] or None, user_id))
            if "project_ids" in data:
                cur.execute("DELETE FROM user_projects WHERE user_id = %s", (user_id,))
                for pid in (data["project_ids"] or []):
                    cur.execute("INSERT INTO user_projects (user_id, project_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, pid))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Notification API
# ---------------------------------------------------------------------------
@app.route("/api/notifications")
@login_required
def get_notifications():
    if not os.environ.get("DATABASE_URL"):
        return jsonify([])
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT n.id, n.case_id, n.message, n.read, n.created_at, c.case_number
                   FROM notifications n JOIN cases c ON n.case_id = c.id
                   WHERE n.user_id = %s ORDER BY n.created_at DESC LIMIT 30""",
                (int(current_user.id),),
            )
            rows = cur.fetchall()
        conn.close()
        return jsonify([{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/count")
@login_required
def notification_count():
    if not os.environ.get("DATABASE_URL"):
        return jsonify({"count": 0})
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id = %s AND read = FALSE", (int(current_user.id),))
            count = cur.fetchone()[0]
        conn.close()
        return jsonify({"count": count})
    except Exception:
        return jsonify({"count": 0})


@app.route("/api/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notif_id):
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read = TRUE WHERE id = %s AND user_id = %s", (notif_id, int(current_user.id)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
def mark_all_read():
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read = TRUE WHERE user_id = %s", (int(current_user.id),))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Corrections API (admin)
# ---------------------------------------------------------------------------
@app.route("/api/corrections")
@login_required
def list_corrections():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT c.id, c.case_number, c.created_at, c.result,
                       c.cashier_instruction_override, c.cashier_instruction_reasoning,
                       c.review_status, c.review_note, c.reviewed_at,
                       u.username AS reviewed_by_username
                FROM cases c
                LEFT JOIN users u ON c.reviewed_by = u.id
                WHERE c.cashier_instruction_override IS NOT NULL
                   OR c.review_status IN ('approved', 'rejected')
                ORDER BY COALESCE(c.reviewed_at, c.created_at) DESC
            """)
            rows = cur.fetchall()
        conn.close()
        return jsonify([{
            "id": r["id"],
            "case_number": r["case_number"],
            "created_at": r["created_at"].isoformat(),
            "original": extract_cashier_instruction(r["result"] or ""),
            "edited": r["cashier_instruction_override"],
            "reasoning": r["cashier_instruction_reasoning"] or "",
            "review_status": r["review_status"],
            "review_note": r["review_note"] or "",
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "reviewed_by": r["reviewed_by_username"] or "",
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Projects API
# ---------------------------------------------------------------------------
@app.route("/api/projects")
@login_required
def list_projects():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, slug, active, created_at FROM projects ORDER BY id")
            rows = cur.fetchall()
        conn.close()
        return jsonify([{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects", methods=["POST"])
@login_required
def create_project():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    name = (data.get("name") or "").strip()
    slug = (data.get("slug") or name.lower().replace(" ", "-")).strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO projects (name, slug) VALUES (%s, %s) RETURNING id", (name, slug))
            pid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": pid, "name": name, "slug": slug})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<int:project_id>", methods=["PUT"])
@login_required
def update_project(project_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if "name" in data:
                cur.execute("UPDATE projects SET name = %s WHERE id = %s", (data["name"], project_id))
            if "active" in data:
                cur.execute("UPDATE projects SET active = %s WHERE id = %s", (data["active"], project_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Work Items API
# ---------------------------------------------------------------------------
@app.route("/api/work-items")
@login_required
def list_work_items():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if current_user.role == "admin":
                cur.execute("""
                    SELECT w.*, p.name AS project_name, u.username AS assigned_username
                    FROM work_items w
                    JOIN projects p ON w.project_id = p.id
                    LEFT JOIN users u ON w.assigned_to = u.id
                    ORDER BY w.due_date ASC, w.created_at ASC
                """)
            else:
                # get user's specialisms
                cur.execute("SELECT specialisms FROM users WHERE id = %s", (int(current_user.id),))
                row = cur.fetchone()
                specialisms = (row["specialisms"] or "all") if row else "all"
                # get user's project ids
                cur.execute("SELECT project_id FROM user_projects WHERE user_id = %s", (int(current_user.id),))
                proj_ids = [r["project_id"] for r in cur.fetchall()]
                if not proj_ids:
                    conn.close()
                    return jsonify([])
                if specialisms == "all":
                    cur.execute("""
                        SELECT w.*, p.name AS project_name, u.username AS assigned_username
                        FROM work_items w
                        JOIN projects p ON w.project_id = p.id
                        LEFT JOIN users u ON w.assigned_to = u.id
                        WHERE w.project_id = ANY(%s)
                        ORDER BY w.due_date ASC, w.created_at ASC
                    """, (proj_ids,))
                else:
                    spec_list = [s.strip() for s in specialisms.split(",")]
                    cur.execute("""
                        SELECT w.*, p.name AS project_name, u.username AS assigned_username
                        FROM work_items w
                        JOIN projects p ON w.project_id = p.id
                        LEFT JOIN users u ON w.assigned_to = u.id
                        WHERE w.project_id = ANY(%s) AND w.task_type = ANY(%s)
                        ORDER BY w.due_date ASC, w.created_at ASC
                    """, (proj_ids, spec_list))
            rows = cur.fetchall()
        conn.close()
        return jsonify([{**dict(r), "due_date": r["due_date"].isoformat(), "created_at": r["created_at"].isoformat()} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items", methods=["POST"])
@login_required
def create_work_item():
    if current_user.role not in ("admin", "uploader"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    project_id = data.get("project_id")
    task_type = data.get("task_type", "completion")
    case_number = (data.get("case_number") or "").strip()
    due_date = data.get("due_date")
    notes = (data.get("notes") or "").strip() or None
    if not all([project_id, case_number, due_date]):
        return jsonify({"error": "project_id, case_number and due_date required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO work_items (project_id, task_type, case_number, due_date, created_by, notes)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (project_id, task_type, case_number, due_date, int(current_user.id), notes),
            )
            wid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": wid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items/upload", methods=["POST"])
@login_required
def upload_work_items():
    if current_user.role not in ("admin", "uploader"):
        return jsonify({"error": "Forbidden"}), 403
    project_id = request.form.get("project_id")
    task_type = request.form.get("task_type", "completion")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    try:
        content = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        inserted = 0
        errors = []
        conn = get_db_conn()
        with conn.cursor() as cur:
            for i, row in enumerate(reader, 1):
                case_num = (row.get("case_number") or row.get("Case Number") or "").strip()
                due = (row.get("due_date") or row.get("Due Date") or "").strip()
                tt = (row.get("task_type") or row.get("Task Type") or task_type).strip()
                notes = (row.get("notes") or row.get("Notes") or "").strip() or None
                if not case_num or not due:
                    errors.append(f"Row {i}: missing case_number or due_date")
                    continue
                cur.execute(
                    """INSERT INTO work_items (project_id, task_type, case_number, due_date, created_by, notes)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (project_id, tt, case_num, due, int(current_user.id), notes),
                )
                inserted += 1
        conn.commit()
        conn.close()
        return jsonify({"inserted": inserted, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items/<int:item_id>", methods=["PUT"])
@login_required
def update_work_item(item_id):
    data = request.get_json()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if "status" in data:
                new_status = data["status"]
                if new_status == "in_progress":
                    cur.execute(
                        "UPDATE work_items SET status=%s, assigned_to=%s WHERE id=%s",
                        (new_status, int(current_user.id), item_id),
                    )
                else:
                    cur.execute("UPDATE work_items SET status=%s WHERE id=%s", (new_status, item_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items/<int:item_id>", methods=["DELETE"])
@login_required
def delete_work_item(item_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM work_items WHERE id = %s", (item_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Dashboard Stats API
# ---------------------------------------------------------------------------
@app.route("/api/dashboard/stats")
@login_required
def dashboard_stats():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if current_user.role == "admin":
                cur.execute("SELECT id FROM projects WHERE active = TRUE")
                proj_ids = [r["id"] for r in cur.fetchall()]
            else:
                cur.execute("SELECT project_id FROM user_projects WHERE user_id = %s", (int(current_user.id),))
                proj_ids = [r["project_id"] for r in cur.fetchall()]

            if not proj_ids:
                conn.close()
                return jsonify([])

            cur.execute("""
                SELECT p.id, p.name,
                    COUNT(w.id) FILTER (WHERE w.status = 'pending') AS pending_total,
                    COUNT(w.id) FILTER (WHERE w.status = 'in_progress') AS in_progress_total,
                    COUNT(w.id) FILTER (WHERE w.status = 'completed') AS completed_total,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'completion' AND w.status = 'pending') AS pending_completions,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'arrears' AND w.status = 'pending') AS pending_arrears,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'annual' AND w.status = 'pending') AS pending_annuals,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'variation' AND w.status = 'pending') AS pending_variations,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'termination' AND w.status = 'pending') AS pending_terminations,
                    MIN(w.due_date) FILTER (WHERE w.status = 'pending') AS next_due
                FROM projects p
                LEFT JOIN work_items w ON w.project_id = p.id
                WHERE p.id = ANY(%s)
                GROUP BY p.id, p.name
                ORDER BY p.id
            """, (proj_ids,))
            rows = cur.fetchall()

            # approved cases per project (lifetime)
            cur.execute("""
                SELECT project_id, COUNT(*) AS approved_count
                FROM cases
                WHERE project_id = ANY(%s) AND review_status = 'approved'
                GROUP BY project_id
            """, (proj_ids,))
            approved_map = {r["project_id"]: r["approved_count"] for r in cur.fetchall()}

        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["approved_cases"] = approved_map.get(r["id"], 0)
            if d["next_due"]:
                d["next_due"] = d["next_due"].isoformat()
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Cases API
# ---------------------------------------------------------------------------
@app.route("/api/cases")
@login_required
def list_cases():
    if not os.environ.get("DATABASE_URL"):
        return jsonify([])
    task_type_filter = request.args.get("task_type")
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if task_type_filter:
                cur.execute(
                    "SELECT id, case_number, created_at, review_status FROM cases WHERE task_type = %s ORDER BY created_at DESC LIMIT 100",
                    (task_type_filter,),
                )
            else:
                cur.execute("SELECT id, case_number, created_at, review_status FROM cases ORDER BY created_at DESC LIMIT 100")
            rows = cur.fetchall()
        conn.close()
        return jsonify([{"id": r["id"], "case_number": r["case_number"], "created_at": r["created_at"].isoformat(), "review_status": r["review_status"]} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/export")
@login_required
def export_cases():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT case_number, created_at, result, cashier_instruction_override, cashier_instruction_reasoning, input_tokens, output_tokens FROM cases ORDER BY created_at DESC")
            rows = cur.fetchall()
        conn.close()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Case Number", "Date", "Cashier Instruction", "Edit Reasoning", "Input Tokens", "Output Tokens"])
        for row in rows:
            cashier = row["cashier_instruction_override"] or extract_cashier_instruction(row["result"] or "")
            writer.writerow([
                row["case_number"],
                row["created_at"].strftime("%d/%m/%Y %H:%M"),
                cashier,
                row["cashier_instruction_reasoning"] or "",
                row["input_tokens"],
                row["output_tokens"],
            ])
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=worked-cases.csv"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>")
@login_required
def get_case(case_id):
    if not os.environ.get("DATABASE_URL"):
        return jsonify({"error": "No database configured"}), 503
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "id": row["id"], "case_number": row["case_number"],
            "created_at": row["created_at"].isoformat(), "result": row["result"],
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
            "cache_creation_tokens": row["cache_creation_tokens"], "cache_read_tokens": row["cache_read_tokens"],
            "cashier_instruction_override": row.get("cashier_instruction_override"),
            "review_status": row.get("review_status"),
            "variation_data": row.get("variation_data"),
            "submitted_by": row.get("submitted_by"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/variation-save", methods=["PUT"])
@login_required
def variation_save(case_id):
    if current_user.role not in ("uploader", "admin", "reviewer"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    section = data.get("section")
    if section not in ("eos", "ie", "reason"):
        return jsonify({"error": "Invalid section"}), 400
    section_data = data.get("data")
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT variation_data FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "Not found"}), 404
            try:
                vd = json.loads(row["variation_data"]) if row["variation_data"] else {}
            except (json.JSONDecodeError, TypeError):
                vd = {}
            vd[section] = section_data
            if "saved" not in vd:
                vd["saved"] = {}
            vd["saved"][section] = True
            cur.execute("UPDATE cases SET variation_data = %s WHERE id = %s", (json.dumps(vd), case_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "saved": vd.get("saved", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/send-for-review", methods=["POST"])
@login_required
def send_for_review(case_id):
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    submitted_by = int(current_user.id)
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT case_number FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "Not found"}), 404
            case_number = row["case_number"]
            cur.execute("UPDATE cases SET review_status = 'under_review' WHERE id = %s", (case_id,))
            cur.execute(
                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                (submitted_by,),
            )
            for (uid,) in cur.fetchall():
                cur.execute(
                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                    (uid, case_id, f"Variation sent for review: {case_number}"),
                )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/action", methods=["POST"])
@login_required
def action_case(case_id):
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE cases SET review_status = 'actioned' WHERE id = %s", (case_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/cashier", methods=["PUT"])
@login_required
def save_cashier_instruction(case_id):
    if current_user.role not in ("reviewer", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    instruction = data.get("instruction", "").strip()
    reasoning = data.get("reasoning", "").strip()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cases SET cashier_instruction_override = %s, cashier_instruction_reasoning = %s WHERE id = %s",
                (instruction, reasoning or None, case_id),
            )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/review", methods=["POST"])
@login_required
def review_case(case_id):
    if current_user.role not in ("reviewer", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    action = data.get("action")
    if action not in ("approve", "reject"):
        return jsonify({"error": "Invalid action"}), 400
    note = data.get("note", "").strip()
    instruction = data.get("instruction")
    status = "approved" if action == "approve" else "rejected"
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if instruction is not None:
                cur.execute(
                    """UPDATE cases SET review_status=%s, review_note=%s,
                       reviewed_by=%s, reviewed_at=NOW(),
                       cashier_instruction_override=%s WHERE id=%s""",
                    (status, note or None, int(current_user.id), instruction, case_id),
                )
            else:
                cur.execute(
                    """UPDATE cases SET review_status=%s, review_note=%s,
                       reviewed_by=%s, reviewed_at=NOW() WHERE id=%s""",
                    (status, note or None, int(current_user.id), case_id),
                )
            if action == "approve":
                cur.execute("SELECT submitted_by, case_number FROM cases WHERE id = %s", (case_id,))
                row = cur.fetchone()
                if row and row["submitted_by"]:
                    cur.execute(
                        "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                        (row["submitted_by"], case_id, f"Variation approved — ready to action: {row['case_number']}"),
                    )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>", methods=["DELETE"])
@login_required
def delete_case(case_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notifications WHERE case_id = %s", (case_id,))
            cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------
@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403

    eos_from_vmoc = request.form.get("eos_from_vmoc", "no").lower() == "yes"
    additional_notes = request.form.get("notes", "").strip()
    case_number = request.form.get("case_number", "").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None
    task_type = request.form.get("task_type", "completion").strip() or "completion"
    work_item_id_raw = request.form.get("work_item_id", "").strip()
    work_item_id = int(work_item_id_raw) if work_item_id_raw.isdigit() else None
    submitted_by = int(current_user.id)

    content = []
    any_document = False

    for field_name, label in DOCUMENT_SLOTS:
        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True
        doc_label = label + (" [VMOC]" if field_name == "eos" and eos_from_vmoc else "")
        content.append({"type": "text", "text": f"--- {doc_label} ({len(pages)} page(s)) ---"})
        for page in pages:
            try:
                image_data, media_type = encode_file(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    if not any_document:
        return jsonify({"error": "Please upload at least one document."}), 400

    trigger_parts = []
    if eos_from_vmoc:
        trigger_parts.append("EOS IS VMOC")
    if additional_notes:
        trigger_parts.append(additional_notes)
    trigger_parts.append("CALCULATE")
    content.append({"type": "text", "text": "\n\n".join(trigger_parts)})

    def generate():
        full_text = []
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=16000,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

                msg = stream.get_final_message()
                usage = msg.usage
                case_id = None

                if case_number and os.environ.get("DATABASE_URL"):
                    try:
                        conn = get_db_conn()
                        with conn.cursor() as cur:
                            cur.execute(
                                """INSERT INTO cases
                                   (case_number, result, input_tokens, output_tokens,
                                    cache_creation_tokens, cache_read_tokens, submitted_by,
                                    project_id, task_type)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                 getattr(usage, "cache_creation_input_tokens", 0),
                                 getattr(usage, "cache_read_input_tokens", 0), submitted_by,
                                 project_id, task_type),
                            )
                            case_id = cur.fetchone()[0]
                            # mark linked work item in_progress → completed
                            if work_item_id:
                                cur.execute(
                                    "UPDATE work_items SET status='in_progress', assigned_to=%s WHERE id=%s",
                                    (submitted_by, work_item_id),
                                )
                            cur.execute(
                                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                                (submitted_by,),
                            )
                            for (uid,) in cur.fetchall():
                                cur.execute(
                                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                                    (uid, case_id, f"New case for review: {case_number}"),
                                )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"Failed to save case: {e}")

                yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"

        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True)
